# This file is part of obs_base.
#
# Developed for the LSST Data Management System.
# This product includes software developed by the LSST Project
# (http://www.lsst.org).
# See the COPYRIGHT file at the top-level directory of this distribution
# for details of code ownership.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.


__all__ = ("RawIngestTask", "RawIngestConfig")

import os.path

# This should really be an error that is caught in daf butler and rethrown with our own
# but it is not, so this exists here pending some error refactoring in daf butler
from sqlalchemy.exc import IntegrityError

from astro_metadata_translator import ObservationInfo
from lsst.afw.image import readMetadata, bboxFromMetadata
from lsst.afw.geom import SkyWcs
from lsst.daf.butler import (DatasetType, StorageClassFactory, Run, DataId, ConflictingDefinitionError,
                             Butler)
from lsst.daf.butler.instrument import (Instrument, updateExposureEntryFromObsInfo,
                                        updateVisitEntryFromObsInfo)
from lsst.geom import Box2D
from lsst.pex.config import Config, Field, ChoiceField
from lsst.pipe.base import Task
from lsst.sphgeom import ConvexPolygon


class IngestConflictError(ConflictingDefinitionError):
    pass


class RawIngestConfig(Config):
    transfer = ChoiceField(
        ("How to transfer files (None for no transfer)."),
        dtype=str,
        allowed={"move": "move",
                 "copy": "copy",
                 "hardlink": "hard link",
                 "symlink": "symbolic (soft) link"},
        optional=True,
    )
    conflict = ChoiceField(
        ("What to do if a raw Dataset with the same data ID as an "
         "ingested file already exists in the Butler's Collection."),
        dtype=str,
        allowed={"ignore": ("Do not add the new file to the Collection.  If "
                            "'stash' is not None, the new file will be "
                            "ingested into the stash Collection instead."),
                 "fail": ("Raise RuntimeError if a conflict is encountered "
                          "(which may then be caught if onError == 'continue')."),
                 },
        optional=False,
        default="ignore",
    )
    stash = Field(
        "Name of an alternate Collection to hold Datasets that lose conflicts.",
        dtype=str,
        default=None,
    )
    onError = ChoiceField(
        "What to do if an error (including fatal conflicts) occurs.",
        dtype=str,
        allowed={"continue": "Warn and continue with the next file.",
                 "break": ("Stop processing immediately, but leave "
                           "already-ingested datasets in the repository."),
                 "rollback": ("Stop processing and attempt to remove aleady-"
                              "ingested datasets from the repository."),
                 },
        optional=False,
        default="continue",
    )
    doAddRegions = Field(
        dtype=bool,
        default=True,
        doc="Add regions when ingesting tasks"
    )
    padRegionAmount = Field(
        dtype=int,
        default=0,
        doc="Pad an image with specified number of pixels before calculating region"
    )


class RawIngestTask(Task):
    """Driver Task for ingesting raw data into Gen3 Butler repositories.

    This Task is intended to be runnable from the command-line, but it doesn't
    meet the other requirements of CmdLineTask or PipelineTask, and wouldn't
    gain much from being one.  It also wouldn't really be appropriate as a
    subtask of a CmdLineTask or PipelineTask; it's a Task essentially just to
    leverage the logging and configurability functionality that provides.

    Each instance of `RawIngestTask` writes to the same Butler and maintains a
    cache of Dimension entries that have already been added to or extracted
    from its Registry.  Each invocation of `RawIngestTask.run` ingests a list
    of files (possibly semi-atomically; see `RawIngestConfig.onError`).

    RawIngestTask may be subclassed to specialize ingest for the actual
    structure of raw data files produced by a particular instrument, but this
    is usually unnecessary because the instrument-specific header-extraction
    provided by the ``astro_metadata_translator`` is usually enough.

    Parameters
    ----------
    config : `RawIngestConfig`
        Configuration for whether/how to transfer files and how to handle
        conflicts and errors.
    butler : `~lsst.daf.butler.Butler`
        Butler instance.  Ingested Datasets will be created as part of
        ``butler.run`` and associated with its Collection.

    Other keyword arguments are forwarded to the Task base class constructor.
    """

    ConfigClass = RawIngestConfig

    _DefaultName = "ingest"

    @classmethod
    def getDatasetType(cls):
        """Return the DatasetType of the Datasets ingested by this Task.
        """
        return DatasetType("raw", ("instrument", "detector", "exposure"),
                           StorageClassFactory().getStorageClass("Exposure"))

    def __init__(self, config=None, *, butler, **kwds):
        super().__init__(config, **kwds)
        self.butler = butler
        self.datasetType = self.getDatasetType()
        self.dimensions = butler.registry.dimensions.extract(["instrument", "detector", "physical_filter",
                                                              "visit", "exposure"])
        # Dictionary of {Dimension: set(DataId)} indicating Dimension entries
        # we know are in the Registry.
        self.dimensionEntriesDone = {k: set() for k in self.dimensions}
        # Cache of instrument instances retrieved from Registry; needed to look
        # up formatters.
        self.instrumentCache = {}
        # (Possibly) create a Run object for the "stash": where we put datasets
        # that lose conflicts.  Note that this doesn't actually add this Run
        # to the Registry; we only do that on first use.
        self.stashRun = Run(self.config.stash) if self.config.stash is not None else None
        self.visitRegions = {}

    def _addVisitRegions(self):
        """Adds a region associated with a Visit to registry.

        Visits will be created using regions for individual ccds that are
        defined in the visitRegions dict field on self, joined against an
        existing region if one exists. The dict field is formatted using
        instrument and visit as a tuple for a key, with values that are a
        list of regions for detectors associated the region.
        """
        for (instrument, visit), vertices in self.visitRegions.items():
            # If there is an existing region it should be updated
            existingRegion = self.butler.registry.expandDataId({"instrument": instrument, "visit": visit},
                                                               region=True).region
            if existingRegion is not None:
                vertices = list(existingRegion.getVertices()) + vertices
            region = ConvexPolygon(vertices)
            self.butler.registry.setDimensionRegion(instrument=instrument, visit=visit, region=region)

    def run(self, files):
        """Ingest files into a Butler data repository.

        This creates any new exposure or visit Dimension entries needed to
        identify the ingested files, creates new Dataset entries in the
        Registry and finally ingests the files themselves into the Datastore.
        Any needed instrument, detector, and physical_filter Dimension entries
        must exist in the Registry before `run` is called.

        Parameters
        ----------
        files : iterable over `str` or path-like objects
            Paths to the files to be ingested.  Will be made absolute
            if they are not already.
        """
        self.butler.registry.registerDatasetType(self.getDatasetType())
        if self.config.onError == "rollback":
            with self.butler.transaction():
                for file in files:
                    self.processFile(os.path.abspath(file))
                if self.config.doAddRegions:
                    self._addVisitRegions()
        elif self.config.onError == "break":
            for file in files:
                self.processFile(os.path.abspath(file))
            if self.config.doAddRegions:
                self._addVisitRegions()
        elif self.config.onError == "continue":
            for file in files:
                try:
                    self.processFile(os.path.abspath(file))
                except Exception as err:
                    self.log.warnf("Error processing '{}': {}", file, err)
            if self.config.doAddRegions:
                self._addVisitRegions()

    def readHeaders(self, file):
        """Read and return any relevant headers from the given file.

        The default implementation simply reads the header of the first
        non-empty HDU, so it always returns a single-element list.

        Parameters
        ----------
        file : `str` or path-like object
            Absolute path to the file to be ingested.

        Returns
        -------
        headers : `list` of `~lsst.daf.base.PropertyList`
            Single-element list containing the header of the first
            non-empty HDU.
        """
        return [readMetadata(file)]

    def buildRegion(self, headers):
        """Builds a region from information contained in a header

        Parameters
        ----------
        headers : `lsst.daf.base.PropertyList`
            Property list containing the information from the header of
            one file.

        Returns
        -------
        region : `lsst.sphgeom.ConvexPolygon`

        Raises
        ------
        ValueError :
            If required header keys can not be found to construct region
        """
        # Default implementation is for headers to be a one element list
        header = headers[0]
        wcs = SkyWcs(header)
        bbox = Box2D(bboxFromMetadata(header))
        if self.config.padRegionAmount > 0:
            bbox.grow(self.config.padRegionAmount)
        corners = bbox.getCorners()
        sphCorners = [wcs.pixelToSky(point).getVector() for point in corners]
        return ConvexPolygon(sphCorners)

    def ensureDimensions(self, file):
        """Extract metadata from a raw file and add exposure and visit
        Dimension entries.

        Any needed instrument, detector, and physical_filter Dimension entries must
        exist in the Registry before `run` is called.

        Parameters
        ----------
        file : `str` or path-like object
            Absolute path to the file to be ingested.

        Returns
        -------
        headers : `list` of `~lsst.daf.base.PropertyList`
            Result of calling `readHeaders`.
        dataId : `DataId`
            Data ID dictionary, as returned by `extractDataId`.
        """
        headers = self.readHeaders(file)
        obsInfo = ObservationInfo(headers[0])

        # Extract a DataId that covers all of self.dimensions.
        fullDataId = self.extractDataId(file, headers, obsInfo=obsInfo)

        for dimension in self.dimensions:
            dimensionDataId = DataId(fullDataId, dimension=dimension)
            if dimensionDataId not in self.dimensionEntriesDone[dimension]:
                # Next look in the Registry
                dimensionEntryDict = self.butler.registry.findDimensionEntry(dimension, dimensionDataId)
                if dimensionEntryDict is None:
                    if dimension.name in ("visit", "exposure"):
                        # Add the entry into the Registry.
                        self.butler.registry.addDimensionEntry(dimension, dimensionDataId)
                    else:
                        raise LookupError(
                            f"Entry for {dimension.name} with ID {dimensionDataId} not found; must be "
                            f"present in Registry prior to ingest."
                        )
                # Record that we've handled this entry.
                self.dimensionEntriesDone[dimension].add(dimensionDataId)
        # Do this after the loop to ensure all the dimensions are added
        if self.config.doAddRegions:
            region = self.buildRegion(headers)
            try:
                self.butler.registry.setDimensionRegion(DataId(fullDataId,
                                                               dimensions=['visit', 'detector', 'instrument'],
                                                               region=region),
                                                        update=False)
                self.visitRegions.setdefault((fullDataId['instrument'], fullDataId['visit']),
                                             []).extend(region.getVertices())
            except IntegrityError:
                # This means that there were already regions for the dimensions in the database, and nothing
                # should be done.
                pass

        return headers, fullDataId

    def ingestFile(self, file, headers, dataId, run=None):
        """Ingest a single raw file into the repository.

        All necessary Dimension entres must already be present.

        Parameters
        ----------
        file : `str` or path-like object
            Absolute path to the file to be ingested.
        headers : `list` of `~lsst.daf.base.PropertyList`
            Result of calling `readHeaders`.
        dataId : `dict`
            Data ID dictionary, as returned by `extractDataId`.
        run : `~lsst.daf.butler.Run`, optional
            Run to add the Dataset to; defaults to ``self.butler.run``.

        Returns
        -------
        ref : `DatasetRef`
            Reference to the ingested dataset.

        Raises
        ------
        ConflictingDefinitionError
            Raised if the dataset already exists in the registry.
        """
        if run is not None and run != self.butler.run:
            butler = Butler(self.butler, run=run)
        else:
            butler = self.butler
        try:
            return butler.ingest(file, self.datasetType, dataId, transfer=self.config.transfer,
                                 formatter=self.getFormatter(file, headers, dataId))
        except ConflictingDefinitionError as err:
            raise IngestConflictError("Ingest conflict on {} {}".format(file, dataId)) from err

    def processFile(self, file):
        """Ingest a single raw data file after extacting metadata.

        This creates any new exposure or visit Dimension entries needed to
        identify the ingest file, creates a new Dataset entry in the
        Registry and finally ingests the file itself into the Datastore.
        Any needed instrument, detector, and physical_filter Dimension entries must
        exist in the Registry before `run` is called.

        Parameters
        ----------
        file : `str` or path-like object
            Absolute path to the file to be ingested.
        """
        headers, dataId = self.ensureDimensions(file)
        # We want ingesting a single file to be atomic even if we are
        # not trying to ingest the list of files atomically.
        with self.butler.transaction():
            try:
                self.ingestFile(file, headers, dataId)
                return
            except IngestConflictError:
                if self.config.conflict == "fail":
                    raise
        if self.config.conflict == "ignore":
            if self.stashRun is not None:
                if self.stashRun.id is None:
                    self.butler.registry.ensureRun(self.stashRun)
                self.log.infof("Conflict on {} ({}); ingesting to stash '{}' instead.",
                               dataId, file, self.config.stash)
                with self.butler.transaction():
                    self.ingestFile(file, headers, dataId, run=self.stashRun)
            else:
                self.log.infof("Conflict on {} ({}); ignoring.", dataId, file)

    def extractDataId(self, file, headers, obsInfo):
        """Return the Data ID dictionary that should be used to label a file.

        Parameters
        ----------
        file : `str` or path-like object
            Absolute path to the file being ingested (prior to any transfers).
        headers : `list` of `~lsst.daf.base.PropertyList`
            All headers returned by `readHeaders()`.
        obsInfo : `astro_metadata_translator.ObservationInfo`
            Observational metadata extracted from the headers.

        Returns
        -------
        dataId : `DataId`
            A mapping whose key-value pairs uniquely identify raw datasets.
            Must have ``dataId.dimensions() <= self.dimensions``, with at least
            instrument, exposure, and detector present.
        """
        toRemove = set()
        if obsInfo.visit_id is None:
            toRemove.add("visit")
        if obsInfo.physical_filter is None:
            toRemove.add("physical_filter")
        if toRemove:
            dimensions = self.dimensions.difference(toRemove)
        else:
            dimensions = self.dimensions
        dataId = DataId(
            dimensions=dimensions,
            instrument=obsInfo.instrument,
            exposure=obsInfo.exposure_id,
            visit=obsInfo.visit_id,
            detector=obsInfo.detector_num,
            physical_filter=obsInfo.physical_filter,
        )
        updateExposureEntryFromObsInfo(dataId, obsInfo)
        if obsInfo.visit_id is not None:
            updateVisitEntryFromObsInfo(dataId, obsInfo)
        return dataId

    def getFormatter(self, file, headers, dataId):
        """Return the Formatter that should be used to read this file after
        ingestion.

        The default implementation obtains the formatter from the Instrument
        class for the given data ID.
        """
        instrument = self.instrumentCache.get(dataId["instrument"])
        if instrument is None:
            instrument = Instrument.factories[dataId["instrument"]]()
            self.instrumentCache[dataId["instrument"]] = instrument
        return instrument.getRawFormatter(dataId)
