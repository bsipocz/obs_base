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

__all__ = ("FitsStampsFormatter", )

import os.path

from lsst.obs.base.formatters.fitsGeneric import FitsGenericFormatter


class FitsStampsFormatter(FitsGenericFormatter):
    """Interface for reading and writing collections of stamps. Inherits
    from fitsGenericFormatter, but allows for only a subset of each stamp
    image to be read.
    """

    def _readFile(self, path, pytype):
        """Read a file from the path in FITS format.
        Parameters
        ----------
        path : `str`
            Path to use to open the file.
        pytype : `class`
            Class to use to read the FITS file.
        Returns
        -------
        data : `object`
            Instance of class `pytype` read from FITS file. None
            if the file could not be opened.
        """
        if not os.path.exists(path):
            return None
        return pytype.readFitsWithOptions(path, options=self.fileDescriptor.parameters)
