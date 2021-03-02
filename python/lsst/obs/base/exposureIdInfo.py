# This file is part of obs_base.
#
# Developed for the LSST Data Management System.
# This product includes software developed by the LSST Project
# (https://www.lsst.org).
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
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

__all__ = ["ExposureIdInfo"]

from lsst.daf.butler import DataCoordinate


class ExposureIdInfo(object):
    """Struct representing an exposure ID and the number of bits it uses.

    Parameters
    ----------
    expId : `int`
        Exposure ID.  Note that this is typically the ID of an
        `afw.image.Exposure`, not the ID of an actual observation, and hence it
        usually either includes a detector component or is derived from SkyMap
        IDs.  For code using the Gen3 butler, this will usually be obtained
        via a `~lsst.daf.butler.DimensionPacker` (see example below).
    expBits : `int`
        Maximum number of bits allowed for exposure IDs of this type.
    maxBits : `int`, optional
        Maximum number of bits available for values that combine exposure ID
        with other information, such as source ID.  If not provided
        (recommended when possible), `unusedBits` will be computed by assuming
        the full ID must fit an an `lsst.afw.table` RecordId field.

    Examples
    --------
    One common use is creating an ID factory for making a source table.
    For example, given a `ExposureIdInfo` instance ``info``,

    .. code-block:: python

        from lsst.afw.table import IdFactory, SourceTable
        sourceIdFactory = IdFactory.makeSource(info.expId, info.unusedBits)
         schema = SourceTable.makeMinimalSchema()
        #...add fields to schema as desired, then...
        sourceTable = SourceTable.make(self.schema, sourceIdFactory)
    
    An `ExposureIdInfo` instance can be obtained from a Gen2 data butler
    ``butler`` and dictionary ``dataId`` that identifies a visit and a detector
    via

    .. code-block:: python

        info = butler.get("expIdInfo", dataId)

    The Gen3 version is

    .. code-block:: python

        expandedDataId = butler.registry.expandDataId(dataId)
        info = ExposureIdInfo.fromDataId(expandedDataID, "visit_detector")

    The first line should be unnecessary for the data IDs passed to
    `~lsst.pipe.base.PipelineTask` methods, as those are already expanded, and
    ``"visit_detector"`` can be replaced by other strings to pack data IDs with
    different dimensions (e.g. ``"tract_patch"`` or ``"tract_patch_band"``);
    see the data repository's dimensions configuration for other options.

    At least one bit must be reserved for the exposure ID, even if there is no
    exposure ID, for reasons that are not entirely clear (this is DM-6664).
    """

    def __init__(self, expId=0, expBits=1, maxBits=None):
        """Construct an ExposureIdInfo

        See the class doc string for an explanation of the arguments.
        """
        expId = int(expId)
        expBits = int(expBits)

        if expId.bit_length() > expBits:
            raise RuntimeError("expId=%s uses %s bits > expBits=%s" % (expId, expId.bit_length(), expBits))

        self.expId = expId
        self.expBits = expBits

        if maxBits is not None:
            maxBits = int(maxBits)
            if maxBits < expBits:
                raise RuntimeError("expBits=%s > maxBits=%s" % (expBits, maxBits))
        self.maxBits = maxBits

    @classmethod
    def fromDataId(cls, dataId, name="visit_detector", maxBits=None):
        """Construct an instance from a fully-expanded data ID.

        Parameters
        ----------
        dataId : `lsst.daf.butler.DataCoordinate`
            A data ID that identifies the dimensions to be packed and contains
            extra information about the maximum values for those dimensions.
            An expanded data ID can be obtained from `Registry.expandDataId`,
            but all data IDs passed to `PipelineTask` methods should already
            be expanded.
        name : `str`, optional
            Name of the packer to use.  The set of available packers can be
            found in the data repository's dimension configuration (see
            ``dimensions.yaml`` in daf_butler for the defaults).
        maxBits : `int`, optional
            Forwarded as the ``__init__`` parameter of the same name.  Should
            usually be unnecessary.
        """
        if not isinstance(DataCoordinate) or not dataId.hasRecords():
            raise RuntimeError(
                "A fully-expanded data ID is required; use "
                "Registry.expandDataId to obtain one."
            )
        expId, expBits = dataId.pack(name, returnMaxBits=True)
        return cls(expId=expId, expBits=expBits, maxBits=maxBits)

    @property
    def unusedBits(self):
        """Maximum number of bits available for non-exposure info `(int)`.
        """
        if self.maxBits is None:
            from lsst.afw.table import IdFactory
            return IdFactory.computeReservedFromMaxBits(self.expBits)
        else:
            return self.maxBits - self.expBits
