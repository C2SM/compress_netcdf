import sys
import os
import argparse
import datetime
import numpy as np
from netCDF4 import Dataset


class PackNetCDF(object):
    # number of values 2 and 4 byte unsigned integers
    outResolutionShort = 2.0**16 - 2
    outResolutionLong = 2.0**32 - 2  # for unknown reason 2**32 produces wrong results
                                     # try it anyway - hvw
    complevel = 9

    # coordinate variables to prevent from compression even if they are 2D
    # TODO automatically find coordinate variables!
    exclude = ('lon', 'lat', 'slon', 'slat', 'slonu', 'slatu', 'slonv',
               'slatv', 'time', 'time_bnds', 'rlon', 'rlat', 'level_bnds',
               'level', 'levels')

    def __init__(self):
        self.fin, self.fout, self.overwrite = self.parse_cmd()
        self.dsin = Dataset(self.fin, 'r')
        self.dsout = Dataset(self.fout, 'w')


    def parse_cmd(self):
        parser = argparse.ArgumentParser(description='Compress a netcdf file by ' +
        'first converting float32 and float64 type2D ,3D and 4D fields into ' +
        'integers with offset and scaling factor and then compressing with zlib ' +
        'compression.')
        parser.add_argument('-o', dest='fout',
                            help='compressed netcdf file', metavar='OUTFILE')
        parser.add_argument('-W', default=False, action='store_true',
                            dest='overwrite', help='replace input file, ' +
                            'overrides -o option.')
        parser.add_argument('fin', help='input file', metavar='INFILE')
        args = vars(parser.parse_args())

        # check input file
        fin = os.path.realpath(args['fin'])
        if not os.path.exists(fin):
            parser.error('input file {} does not exist.'.format(fin))
        dir_in = os.path.dirname(fin)

        # check output file
        if args['overwrite']:
            fout = fin + '.tmp'
        else:
            try:
                # output file specified
                fout = os.path.realpath(args['fout'])
                if not os.path.exists(os.path.dirname(fout)):
                    parser.error('path to output file {} does not exist.'
                                 .format(fout))
            except AttributeError:
                # no output file specified
                dir_out = os.path.join(dir_in, 'compress')
                if not os.path.exists(dir_out):
                    print('creating {}'.format(dir_out))
                    os.mkdir(dir_out)
                fout = os.path.join(dir_out, os.path.basename(fin))
        return((fin, fout, args['overwrite']))

    def update_history_att(self):
        thishistory = (datetime.datetime.now().ctime() +
                       ': ' + ' '.join(sys.argv))
        try:
            newatt = "{}\n{}".format(thishistory, self.ds.getncattr('history'))
            #  separating new entries with "\n" because there is an undocumented
            #  feature in ncdump that will make it look like the attribute is an
            #  array of strings, when in fact it is not.
        except AttributeError:
            newatt = thishistory
        self.ds.setncattr('history', newatt)
        return(newatt)

    def select_vars(self):
        '''Select variables that are going to be packed'''
        v_sel = [x for x in self.ds.variables.iteritems()
                 if (x[0] not in P.exclude) and
                 (x[1].ndim >= 2) and
                 (x[1].dtype in ['float64', 'float32', 'uint32', 'uint16'])]
        self.selected_vars = v_sel


    def cp_all(self, compressvars=None, compresshook=None):
        '''
        Copy content of netCDF-structure from self.dsin to self.dsout. Replace
        variables in <compressvars> with the output of compresshook(<variable>).
        '''
        # Global attributes
        glob_atts = dict([(x, self.dsin.getncattr(x))
                          for x in self.dsin.ncattrs()])
        self.dsout.setncatts(glob_atts)
        # dimensions
        dim_sizes = [None if x.isunlimited() else len(x)
                     for x in self.dsin.dimensions.values()]
        dimensions = zip(self.dsin.dimensions.keys(), dim_sizes)
        for d in dimensions:
            self.dsout.createDimension(d[0], size=d[1])
        # variables
        for v in self.dsin.variables.itervalues():
            print("processing variable: {}".format(v.name)),
            if compressvars is None or v.name not in compressvars:
                print("copy")
                v_new = self.dsout.createVariable(v.name, v.dtype, v.dimensions)
                atts_new = dict([(x, v.getncattr(x)) for x in v.ncattrs()])
                v_new.setncatts(atts_new)
            else:
                v_new = compresshook(v)
            v_new[:] = v[:]

    def check_values(self, v):
        '''
        Checks whether values of <v> are identical for self.dsin
        and self.dsout.
        '''
        assert(np.all(self.dsin.variables[v][:] == self.dsout.variables[v][:]))
        return("Value check for {} passed.".format(v))

    def compress(self, v):
        # check range, computed offset and scaling, and check if variable is
        # well behaved (short integer ok) or highly skewed (long integer necessary)
        minVal = np.min(v[:])
        maxVal = np.max(v[:])
        meanVal = np.mean(v[:])
        if np.min(meanVal - minVal,
                  maxVal - meanVal) < (maxVal - minVal) / 1000:
            intType = np.dtype('uint32')
            outres = self.outResolutionLong
            fillval = np.uint32(2**32 - 1)
        else:
            intType = np.dtype('uint16')
            outres = self.outResolutionShort
            fillval = np.uint16(2**16 - 1)
        print("Packing variable {} [min:{}, mean:{}, max:{}] <{}> into <{}>"
              .format(v.name, minVal, meanVal, maxVal, v.dtype, intType))

        # choose chunksize: The horizontal domain (last 2 dimensions)
        # is one chunk. That the last 2 dimensions span the horizontal
        # domain is a COARDS convention, which we assume here nonetheless.
        chunksizes = tuple([1]*(len(v.dimensions) - 2) +
                           [len(self.dsin.dimensions[x])
                            for x in v.dimensions[-2:]])
        v_new = self.dsout.createVariable(v.name, intType, v.dimensions,
                                          zlib=True, complevel=self.complevel,
                                          chunksizes=chunksizes,
                                          fill_value=fillval)
        scale_factor = (maxVal - minVal) / outres or 1
        v_new.setncattr('scale_factor', scale_factor)
        v_new.setncattr('add_offset', minVal)
        v_new.setncattr('_FillValue', fillval)
        v_new.set_auto_maskandscale(True)
        # copy untouched attributes
        att_cp = dict([(x, v.getncattr(x)) for x in v.ncattrs()
                       if x not in v_new.ncattrs()])
        v_new.setncatts(att_cp)
        return(v_new)

    def get_coordvars(self, dimension=None, type=None):
        '''
        Returns all variable names that represent coordinates. Restrict
        to time, easting, northing by specifying
          dimension='T',
          dimension='Z',
          dimension='X',
          dimension='Y',
        respectively.
        Specify
          <type>='dim'
        to get only dimension variables, or
          <type>='aux'
        to get only auxiliary coordinate variables.
        '''
        def isT(v):
            print(v.name)
            try:
                if v.getncattr('axis') == 'T':
                    return(True)
            except:
                pass
            try:
                if (v.getncattr('units').split(' ')[0]
                    in ['common_year', 'common_years', 'year', 'years', 'yr', 'a', 'month', 'months', 'week',
                        'weeks', 'day', 'days', 'd', 'hour', 'hours', 'hr',
                        'h', 'minute', 'minutes', 'min', 'second', 'seconds',
                        's', 'sec']):
                    return(True)
            except:
                pass
            return(False)

        # def isZ(self, atts):
        # def isX(self, atts):
        # def isY(self, atts):
        

    # if overwrite:
    #     os.rename(fout,fin)

if __name__ == "__main__":
    P = PackNetCDF()
    print (P.fin, P.fout, P.overwrite)
    P.cp_all(compressvars='pr', compresshook=P.compress)
    # print(P.check_values('pr'))
    P.dsout.close()
    P.dsin.close()
    

# run compress_netcdf.py test_unpacked.nc -o test_packed.nc

