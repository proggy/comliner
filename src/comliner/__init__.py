#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright notice
# ----------------
#
# Copyright (C) 2013-2023 Daniel Jung
# Contact: proggy-contact@mailbox.org
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation; either version 2 of the License, or (at your option)
# any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for
# more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 59 Temple Place, Suite 330, Boston, MA 02111-1307 USA.
#
"""comliner - Command Line Interface Wrapper for Python Functions

Wrap your Python functions with a simple command line interface, so that they
can be run directly from the command line, just like any shell command.  This
is achieved by simply adding a decorator in front of the function and creating
a small standardized executible script.  The commandline interface is powered
by the *optparse* module, and data can optionally be loaded from and saved
to HDF5 files, corresponding to the function arguments or return values.

Simple example:

    >>> # file "my_module.py"
    >>> from comliner import Comliner, list_of
    >>> @Comliner(inmap=dict(x='$@/dset'), preproc=dict(x=list_of(float)))
    >>> def mean(x):
    >>>     return sum(x)/len(x)

Now, you can create a small executable script with the following content:

    >>> import sys, my_module
    >>> sys.exit(my_module._mean())

The way the Comliner is configured in this example, it expects you to specify
a bunch of HDF5 data files, where each file has a scalar dataset called *dset*,
and the function will be called with the list [x1, x2, x3, ...] (its length
corresponds to the number of files). By default, the result of the function
goes to STDOUT (displayed on the screen). However, the behavior can be
configured in various ways.
"""
__version__ = '0.4.1'

import inspect
import os
import sys
import time
import h5py #import h5obj
from tqdm import tqdm #import progress
import clitable
from columnize import columnize
from . import dummy
#from functools import wraps ## use this? probably not, as our "wrapper" is a
                             ## completely different function with another interface

try:
    import optparse2 as optparse
except ImportError:
    import optparse

# monkey patch until inspect becomes Python 3 compatible
if not hasattr(inspect, 'getargspec'):
    inspect.getargspec = inspect.getfullargspec

class Comliner(object):
    """Implement the Comliner decorator.
    """

    def __init__(self, inmap=None, outmap=None, preproc=None, postproc=None,
                 opttypes=None, optdoc=None, shortopts=None, longopts=None,
                 prolog='', epilog='', usage='', version='', wrapname='',
                 overwrite=None, bar=False, stdin_sep=None, stdout_sep=None,
                 first=None, last=None, omit_missing=False):
                 # close_gaps=False
        """Initialize and configure the Comliner decorator.
        """

        ### do this later?
        if prolog and prolog.strip()[-1] != '\n':
            prolog += '\n'
        try:
            prog = sys.argv[0].rsplit('/', 1)[1]
        except:
            prog = ''
        #if isinstance(outmap, str): outmap = [outmap]

        # store configuration
        self.inmap = dict(inmap) if inmap else dict()
        self.outmap = dict(outmap) if outmap else dict()
        self.preproc = preproc or dict()
        self.postproc = postproc or dummy.function1
        self.opttypes = opttypes or dict()
        self.optdoc = optdoc or dict()  # documentation for keyword args
        self.shortopts = shortopts or dict()  # short option names, e.g. -h
        self.longopts = longopts or dict()  # long option names, e.g. --help
        self.prolog = prolog  # help text shown in front of doc string
        self.epilog = epilog  # help text shown after option list
        self.usage = usage  # usage, e.g. prog [filenames ...] [-s] ...
        self.version = version or ' '
        self.wrapname = wrapname  # set custom name for wrapper function
        self.bar = bar
        self.prog = prog  # name of the executable script
        self.stdin_sep = stdin_sep or ' '  # separates columns in STDIN
        self.stdout_sep = stdout_sep or ' '  # separates columns in STDOUT
        self._stdin_eof = False  # flag for STDIN received EOF
        self.parallel = False
        self.overwrite = overwrite
        self.first = first or dummy.function
        self.last = last or dummy.function
        self.omit_missing = omit_missing
        #self.close_gaps = close_gaps

    def __call__(self, func):

        """When the decorator is applied to the user's function, this method is
        called with the function object. It will not touch the function object
        itself and return it in the end. But an additional wrapper function
        will be created in the same module which has the same name as the
        user's function but with a leading underscore. Further more, the
        wrapper function will have the attribute "__comliner__" which is set to
        True. In this way, it may be checked which of the functions defined in
        a module are Comliner wrappers. The executable script is supposed to
        call that wrapper function, i.e. using
        "sys.exit(my_module._my_wrapper())".
        """

        # inspect function
        fmodule = inspect.getmodule(func)
        fname = func.__name__
        fdoc = func.__doc__ or ''
        mname = fmodule.__name__
        fdoc = '\n'.join([s.strip() for s in fdoc.split('\n')])
        #fversion = getattr(self, 'version') \
                   #or time.ctime(os.path.getmtime(fmodule.__file__))
        self.fargnames, self.fvarargsname, self.fvarkwname, defaults, _, _, _ \
            = inspect.getargspec(func)

        # make defaults dictionary
        defaults = list(defaults) if defaults is not None else []
        self.fdefaults = {}
        for fargname, default in zip(self.fargnames[::-1], defaults[::-1]):
            self.fdefaults[fargname] = default
        if self.fvarargsname:
            self.fdefaults[self.fvarargsname] = []
        if self.fvarkwname:
            self.fdefaults[self.fvarkwname] = {}

        # determine which arguments are required, because they don't have a
        # default value
        self.frequired = []
        for fargname in self.fargnames:
            if fargname not in self.fdefaults:
                self.frequired.append(fargname)

        # determine name of the wrapper function
        wrapname = self.wrapname or '_'+fname

        #@wraps(func) ## use this? probably not, as our "wrapper" is a whole
                      ## other function with another interface
        class Wrapper(object):
            __comliner__ = True  # so functions can easily be identified as comliners

            def __call__(wself):

                """The executable script associated with the function is
                supposed to call this function. It is not expecting any
                arguments (the command line arguments will be obtained later)
                and is returning the exit status.
                """

                # first of all, initialize time measurement
                self.time0 = time.time()
                self.date_start = time.ctime()
                self.timings = dict(load=[], preproc=[], call=[], postproc=[],
                                    save=[], loop=[], time0=self.time0)

                # determine input argument mapping
                # automatically add required arguments to the mapping
                for ind, reqarg in enumerate(self.frequired):
                    if reqarg not in self.inmap:
                        mapping = '$%i' % ind
                        if self.any_startswith(self.inmap.values(), mapping):
                            self.raise_reqarg_not_in_inmap(reqarg)
                        self.inmap[reqarg] = mapping

                # automatically add varargs and varkw to mapping
                if self.fvarargsname and self.fvarargsname not in self.inmap \
                        and not self.fargnames \
                        and not self.any_startswith(self.inmap.values(), '$@'):
                    self.inmap[self.fvarargsname] = '$@'
                if self.fvarkwname and self.fvarkwname not in self.inmap \
                        and not self.fargnames and not self.fvarargsname \
                        and not self.any_startswith(self.inmap.values(), '$0'):
                    self.inmap[self.fvarkwname] = '$0'

                # determine execution mode
                # (independent or not, or in other words, sequential or
                # all-at-once)
                indep = self.get_indep(self.inmap, self.outmap)
                ### MAYBE DETERMINE indep AFTER tninargs AND tnoutargs (less
                ### tedious)

                # get theoretical number of input and output arguments
                # (either a fixed non-negative number or 'inf')
                tninargs = self.get_tninargs(self.inmap, indep)
                tnoutargs = self.get_tnoutargs(self.outmap, tninargs, indep)

                # generate suitable usage message
                if not self.usage:
                    self.usage = self.auto_usage(tninargs, tnoutargs, indep,
                                                 self.inmap, self.outmap)

                # define command line interface
                description = self.prolog+'\n'+fdoc if self.prolog else fdoc
                op = optparse.OptionParser(usage=self.usage,
                                           epilog=self.epilog,
                                           description=description,
                                           prog=self.prog,
                                           version=self.version)

                # define general comliner options
                og = optparse.OptionGroup(op, 'Comliner configuration and ' +
                                              'debugging options')
                og.add_option('-I', '--info', default=False,
                              action='store_true',
                              help='show comliner information and exit')
                og.add_option('--info-fdefaults', default=False,
                              action='store_true',
                              help='show function default arguments and exit')
                og.add_option('--info-preproc', default=False,
                              action='store_true',
                              help='show preprocessors and exit')
                og.add_option('--info-postproc', default=False,
                              action='store_true',
                              help='show postprocessors and exit')
                og.add_option('--info-inmap', default=False,
                              action='store_true',
                              help='show input mapping and exit')
                og.add_option('--info-predata', default=False,
                              action='store_true',
                              help='show predata and exit')
                og.add_option('--info-indata', default=False,
                              action='store_true',
                              help='show indata and exit')
                og.add_option('--info-inargs', default=False,
                              action='store_true', help='show inargs and exit')
                og.add_option('--info-inkwargs', default=False,
                              action='store_true',
                              help='show inkwargs and exit')
                og.add_option('--info-outdata', default=False,
                              action='store_true',
                              help='show outdata and exit')
                og.add_option('--info-postdata', default=False,
                              action='store_true',
                              help='show postdata and exit')
                og.add_option('--info-outmap', default=False,
                              action='store_true',
                              help='show output mapping and exit')
                og.add_option('-O', '--overwrite', default=False,
                              action='store_true',
                              help='overwrite existing datasets')
                og.add_option('--no-overwrite', default=False,
                              action='store_true',
                              help='do not overwrite existing datasets')
                og.add_option('-B', '--bar', default=False,
                              action='store_true',
                              help='show progress bar (only in sequential ' +
                                   'or parallel execution mode)')
                og.add_option('--no-bar', default=False, action='store_true',
                              help='do not show progress bar')
                og.add_option('-F', '--cmdfile', default=None, type=str,
                              help='save command line to the given text file')
                og.add_option('-C', '--cmd', default=False,
                              action='store_true',
                              help='save command line to the text file ' +
                                   '"X.cmd", where X is the name of the ' +
                                   'program')
                og.add_option('-E', '--must-exist', dest='must_exist',
                              default=False, action='store_true',
                              help='output files must exist, skip otherwise')
                og.add_option('-T', '--timings', default=False,
                              action='store_true', help='show timings')
                og.add_option('--inmap', help='modify input mapping')
                og.add_option('--outmap', help='modify output mapping')
                og.add_option('--insep', help='set STDIN column separator')
                og.add_option('--outsep', help='set STDOUT column separator')
                og.add_option('-M', '--omit-missing', default=False,
                              action='store_true',
                              help='omit missing input datasets')
                og.add_option('--no-omit-missing', default=False,
                              action='store_true',
                              help='do not omit missing input datasets')
                #og.add_option('-G', '--close-gaps', default=False,
                #        action='store_true',
                #        help='only execute if output datasets do not exist')
                #og.add_option('--no-close-gaps', default=False,
                #        action='store_true',
                #        help='execute regardless of whether output ' +
                #                'datasets exist')
                #og.add_option('-P', '--parallel', default=False,
                        #action='store_true', help='run parallel threads')
                #og.add_option('--no-parallel', default=False,
                        #action='store_true',
                        #help='do not run parallel threads')
                #og.add_option('-N', '--num_threads', default=0,
                        #action='store_true',
                        #help='number of parallel threads. If smaller than ' +
                               #'one, detect and use number of CPU cores')
                op.add_option_group(og)

                # turn function arguments with default values into options
                self.optnames = []
                for argname, default in self.fdefaults.items():
                    if argname in self.inmap \
                            and isinstance(self.inmap[argname], str) \
                            and self.startswith_one_of(self.inmap[argname],
                                                       '$%#'):
                        continue

                    shortopt = '-'+str(self.shortopts.get(argname, argname[0]))
                    if len(shortopt[1:]) != 1:
                        self.error_shortopt(shortopt)
                    longopt = '--'+str(self.longopts.get(argname, argname))
                    defaulttype = type(default) if default is not None else str
                    opttype = self.opttypes.get(argname, defaulttype)
                    if opttype in (list, tuple, dict):
                        opttype = str
                    opttypename = getattr(opttype, '__name__')
                    if opttype is bool:
                        action = 'store_false' if default else 'store_true'
                        opttype = None
                    else:
                        action = None
                    opthelp = self.optdoc.get(argname,
                                              'keyword argument "%s", type %s'
                                              % (argname, opttypename))
                    op.add_option(shortopt, longopt, type=opttype,
                                  default=default, action=action, help=opthelp,
                                  dest=argname)

                    self.optnames.append(argname)

                # parse command line
                self.opts, args = op.parse_args()

                # update input mapping according to --inmap option
                self.inmap = self.update_inmap(self.inmap)

                # expand keyword names in input mapping
                self.inmap, self.fdefaults = self.expand_inmap(self.inmap,
                                                               self.fdefaults)

                # update comliner configuration
                if self.opts.overwrite:
                    self.overwrite = True
                if self.opts.no_overwrite:
                    self.overwrite = False
                if self.opts.bar:
                    self.bar = True
                if self.opts.no_bar:
                    self.bar = False
                #if self.opts.parallel:
                    #self.parallel = True
                #if self.opts.no_parallel:
                    #self.parallel = False
                if self.opts.insep:
                    self.stdin_sep = self.opts.insep
                if self.opts.outsep:
                    self.stdout_sep = self.opts.outsep
                if self.opts.omit_missing:
                    self.omit_missing = True
                if self.opts.no_omit_missing:
                    self.omit_missing = False

                # show certain information about the comliner and exit
                if self.opts.info_fdefaults:
                    return self.display_keywords(self.fdefaults)
                if self.opts.info_preproc:
                    return self.display_keywords(self.preproc)
                if self.opts.info_postproc:
                    return self.display_object(self.postproc)
                if self.opts.info_inmap:
                    return self.display_keywords(self.inmap)
                if self.opts.info_outmap and self.outmap:
                    self.outmap = self.update_outmap(self.outmap)
                    self.outmap, self.fdefaults \
                        = self.expand_outmap(self.outmap, self.fdefaults)
                    return self.display_keywords(self.outmap)

                # determine execution mode ("once", "para", or "seq")
                # (all-at-once, parallel, or sequential)
                exec_mode = self.get_exec_mode(tninargs, tnoutargs, indep,
                                               self.parallel)

                # show comliner information and exit
                if self.opts.info:
                    print('%s.%s' % (mname, wrapname), end=" ")
                    print('%s.%s' % (mname, fname), end=" ")
                    #print('%s>%s' % (self.informat, self.outformat), end=" ")
                    print('%s>%s' % (tninargs, tnoutargs), end=" ")
                    print('para' if self.parallel else ('indep' if indep else 'dep'), end=" ")
                    print(exec_mode)
                    return 0

                # divide argument list (input/output files)
                inargs, outargs = self.divide_args(args, tninargs, tnoutargs, indep)

                ### I think in sequential mode, this should be placed elsewhere
                # skip if output files do not exist
                if self.opts.must_exist:
                    for mapping in self.outmap.values():
                        if mapping.startswith('%') and '/' in mapping:
                            argind = self.get_argind(mapping, symbol='%')
                            if argind is None:
                                continue  # support this as well (%@/...)
                            filename = outargs[argind]
                            if not os.path.isfile(filename):
                                return 0

                # close gaps, only execute if output datasets do not yet exist
                #if self.skip_on_close_gaps(self.outmap):
                #    return 0
                ##if self.opts.close_gaps:
                ##    for mapping in self.outmap.values():
                ##        if startswith_one_of(mapping, '$%'):
                ##            argind = self.get_argind(mapping,
                ##                    symbol=mapping[0])
                ##            if argind is None:
                ##                continue ### support this as well (%@ and $@)
                ##            filename = outargs[argind]
                ##            if not os.path.isfile(filename):
                ##                continue
                ##            dsetname = mapping.split('/', 1)[1]
                ##            with h5obj.File(filename, 'r') as f:
                ##                if dsetname in f:
                ##                    # skip
                ##                    return 0

                self.timings['prepare'] = time.time()-self.time0

                # user-defined actions to do first (before entering execution)
                timestep = time.time()
                self.first()
                self.timings['first'] = time.time()-timestep

                # execute
                if exec_mode == 'once':
                    ex = self.exec_once(inargs, outargs, self.opts, func)
                elif exec_mode == 'seq':
                    ex = self.exec_seq(inargs, outargs, self.opts, func, indep,
                                       tninargs, tnoutargs)
                elif exec_mode == 'para':
                    raise NotImplementedError('parallel execution not ' +
                                              'implemented')
                else:
                    self.raise_exec_mode()

                # user-defined actions to do last (after execution)
                timestep = time.time()
                self.last()
                self.timings['last'] = time.time()-timestep

                # write command line to log file
                if self.opts.cmd or self.opts.cmdfile:
                    logfile = self.opts.cmdfile or self.prog+'.cmd'
                    with open(logfile, 'w') as f:
                        f.write(self.prog+' '+' '.join(sys.argv[1:])+'\n')

                # end time measurements
                self.timings['total'] = time.time()-self.time0
                if self.opts.timings:
                    print(print_timings(self.timings))

                return ex

            def __repr__(wself):
                return '<Comliner wrapper for %s.%s>' % (fmodule.__name__, fname)

        # put comliner into module
        wrapper = Wrapper()
        wrapper.__doc__ = self.prolog + '\n' + (func.__doc__ or '')
        wrapper.__name__ = wrapname
        if hasattr(fmodule, wrapname):
            if self.wrapname:
                self.raise_wrapname_exists(wrapname)
            num = 2
            while hasattr(fmodule, wrapname+str(num)):
                num += 1
            wrapname += str(num)
        setattr(fmodule, wrapname, wrapper)

        # return original function (it has not been altered in any way)
        return func

    def exec_once(self, inargs, outargs, opts, func):
        """Load and save all data, function has access to all the data at
        once.
        """

        time0 = time.time()

        # load data
        timestep = time.time()
        predata = self.fdefaults.copy()
        optvalues = dict((optname, getattr(opts, optname))
                         for optname in self.optnames)
        predata.update(optvalues)
        loaded = self.load_indata_once(inargs, self.inmap)
        predata.update(loaded)
        if self.opts.info_predata:
            return self.display_keywords(predata)
        self.timings['load'].append(time.time()-timestep)

        # apply preprocessor
        timestep = time.time()
        indata = self.apply_preproc(predata)
        if self.opts.info_indata:
            return self.display_keywords(indata)
        self.timings['preproc'].append(time.time()-timestep)

        # prepare data for function call
        finargs, finkwargs = self.split_indata(indata)
        if self.opts.info_inkwargs:
            return self.display_keywords(finkwargs)
        if self.opts.info_inargs:
            return self.display_list(finargs)

        # call function
        timestep = time.time()
        outdata = func(*finargs, **finkwargs)
        if self.opts.info_outdata:
            return self.display_object(outdata)
        self.timings['call'].append(time.time()-timestep)

        # apply postprocessor
        timestep = time.time()
        postdata = self.apply_postproc(outdata)
        if self.opts.info_postdata:
            return self.display_object(postdata)
        self.timings['postproc'].append(time.time()-timestep)

        self.outmap = self.auto_outmap(self.outmap, postdata)
        self.outmap = self.update_outmap(self.outmap)
        self.outmap, self.fdefaults = self.expand_outmap(self.outmap,
                                                         self.fdefaults)
        if self.opts.info_outmap:
            return self.display_keywords(self.outmap)

        # save data
        timestep = time.time()
        ex = self.save_outdata_once(outargs, self.outmap, postdata, inargs,
                                    indata)
        self.timings['save'].append(time.time()-timestep)

        self.timings['loop'].append(time.time()-time0)
        return ex

    def exec_seq(self, inargs, outargs, opts, func, indep,
                 tninargs, tnoutargs):
        """Load and save data one by one, function works on each chunk
        (argument or argument pair or file or file pair or STDIN/STDOUT line)
        separately.
        """
        self._stdin_eof = False  # initialize flag that EOF has been received

        # calculate number of loops
        nloop = 0
        if tninargs != 'inf':
            nloop = tninargs
        if tnoutargs != 'inf' and tnoutargs > tninargs:
            nloop = tnoutargs
        if not tninargs and not tnoutargs:
            nloop = 1
        if len(inargs) > nloop:
            nloop = len(inargs)
        if len(outargs) > nloop:
            nloop = len(outargs)
        eternal = self.any_startswith(self.inmap.values(), '#@') \
            or indep and self.any_startswith(self.inmap.values(), '#')

        with tqdm(total=nloop, disable=not self.bar) as bar: #progress.Bar(nloop, verbose=self.bar) as bar:
            i = 0
            while i < nloop or eternal:
                time0 = time.time()

                inarg = inargs[i] if i < len(inargs) else None
                outarg = outargs[i] if i < len(outargs) else None

                # load data
                timestep = time.time()
                predata = self.fdefaults.copy()
                optvalues = dict((optname, getattr(opts, optname))
                                 for optname in self.optnames)
                predata.update(optvalues)
                loaded = self.load_indata_seq(inarg, self.inmap)
                if self._stdin_eof:
                    break
                predata.update(loaded)
                if self.opts.info_predata:
                    return self.display_keywords(predata)
                self.timings['load'].append(time.time()-timestep)

                # apply preprocessor
                timestep = time.time()
                indata = self.apply_preproc(predata)
                if self.opts.info_indata:
                    return self.display_keywords(indata)
                self.timings['preproc'].append(time.time()-timestep)

                # prepare data for function call
                finargs, finkwargs = self.split_indata(indata)
                if self.opts.info_inkwargs:
                    return self.display_keywords(finkwargs)
                if self.opts.info_inargs:
                    return self.display_list(finargs)

                # call function
                timestep = time.time()
                outdata = func(*finargs, **finkwargs)
                if self.opts.info_outdata:
                    return self.display_object(outdata)
                self.timings['call'].append(time.time()-timestep)

                # apply postprocessor
                timestep = time.time()
                postdata = self.apply_postproc(outdata)
                if self.opts.info_postdata:
                    return self.display_object(postdata)
                self.timings['postproc'].append(time.time()-timestep)

                self.outmap = self.auto_outmap(self.outmap, postdata)
                self.outmap = self.update_outmap(self.outmap)
                self.outmap, self.fdefaults = \
                    self.expand_outmap(self.outmap, self.fdefaults)
                if self.opts.info_outmap:
                    return self.display_keywords(self.outmap)

                # save data
                timestep = time.time()
                self.save_outdata_seq(outarg, self.outmap, postdata, inarg,
                                      indata)
                self.timings['save'].append(time.time()-timestep)

                bar.step()
                i += 1

                self.timings['loop'].append(time.time()-time0)

        # return positive exit status to the executable script
        return 0

    def auto_usage(self, tninargs, tnoutargs, indep, inmap, outmap):
        """Generate a suitable usage string based on comliner configuration.
        """
        
        # input part
        if indep:
            if self.any_startswith(inmap.values(), '$0/'):
                if self.any_startswith(outmap.values(), '$0/'):
                    inpart = '[FILE_1 [FILE_2 [...]]]'
                else:
                    inpart = '[INPUT_FILE_1 [INPUT_FILE_2 [...]]]'
            elif '$0' in inmap.values():
                for argname, mapping in inmap.items():
                    if isinstance(mapping, str) and mapping == '$0':
                        inpart = '[%s_1 [%s_2 [...]]]' % ((argname.upper(),)*2)
                        break
                    else:
                        inpart = ''
                else:
                    inpart = ''
            else:
                inpart = ''
        else:
            if self.any_startswith(inmap.values(), '$@/'):
                if self.any_startswith(outmap.values(), '$@/'):
                    inpart = '[FILE_1 [FILE_2 [...]]]'
                else:
                    inpart = '[INPUT_FILE_1 [INPUT_FILE_2 [...]]]'
            elif '$@' in inmap.values():
                for argname, mapping in inmap.items():
                    if isinstance(mapping, str) and mapping == '$@':
                        inpart = '[%s_1 [%s_2 [...]]]' % ((argname.upper(),)*2)
                        break
                else:
                    inpart = ''
            else:
                mappings = {}
                for argname, mapping in inmap.items():
                    if isinstance(mapping, str) \
                            and mapping.startswith('$'):
                        mappings[mapping] = argname
                keys = mappings.keys()
                keys.sort()
                inpartlist = []
                for key in keys:
                    argname, mapping = mappings[key], key
                    if '/' in mapping:
                        index = mapping.index('/')
                        if self.any_startswith(outmap.values(),
                                               mapping[:(index+1)]):
                            inpartlist.append('FILE')
                        else:
                            inpartlist.append('INPUT_FILE')
                    else:
                        inpartlist.append(argname.upper())
                inpart = ' '.join(inpartlist)

        # output part
        if indep:
            if self.any_startswith(outmap.values(), '%0/'):
                outpart = '[OUTPUT_FILE_1 [OUTPUT_FILE_2 [...]]]'
            else:
                outpart = ''
        else:
            ### to do: support $@ and %@, like in input part above
            indices = set()
            for mapping in outmap.values():
                if isinstance(mapping, str) \
                        and mapping.startswith('%') and '/' in mapping:
                    argind = self.get_argind(mapping, symbol='%')
                    if argind is None:
                        self.raise_argind(mapping)
                    indices.add(argind)
            indices = list(indices)
            indices.sort()
            if len(indices) > 1:
                outpart = ' '.join(['OUTPUT_FILE_%i' % i
                                    for i in range(len(indices))])
            elif len(indices) == 1:
                outpart = 'OUTPUT_FILE'
            else:
                outpart = ''

        # put it together
        if inpart and outpart:
            inpart += ' '
        return '%prog [options] '+inpart+outpart

    def auto_outmap(self, outmap, outdata):
        """Automatically fill output mapping with standard mappings if it is
        still empty.
        """
        if outdata is None:
            return outmap
        if not outmap:
            if type(outdata) is tuple:
                return dict([(i, '#0/%i' % i) for i in range(len(outdata))])
                # return tuple(['#0/%i' % index for index in
                # range(len(outdata))])
            else:
                return {0: '#0'}
        return outmap

    def update_inmap(self, inmap):
        if self.opts.inmap:
            kwpairs = self.opts.inmap.split(',')
            for kwpair in kwpairs:
                if kwpair.count('=') != 1:
                    self.error_inmap_option()
                argname, mapping = kwpair.split('=')
                if self.startswith_one_of(mapping, '$%#'):
                    self.inmap[argname] = mapping
                else:
                    try:
                        self.inmap[argname] = eval(mapping)
                    except NameError:
                        self.inmap[argname] = mapping
        return inmap

    def update_outmap(self, outmap):
        """Update output mapping with user-defined content from the --outmap
        command line option.
        """
        if self.opts.outmap:
            kwpairs = self.opts.outmap.split(',')
            for kwpair in kwpairs:
                if kwpair.count('=') != 1:
                    self.error_outmap_option()
                argname, mapping = kwpair.split('=')
                try:
                    argname = int(argname)
                except:
                    pass
                if self.startswith_one_of(mapping, '$%#'):
                    outmap[argname] = mapping
                else:
                    try:
                        mapping = eval(mapping)
                    except NameError:
                        pass
                    if mapping is None:
                        if argname in outmap:
                            del outmap[argname]
                    else:
                        outmap[argname] = mapping
        return outmap

    def expand_inmap(self, inmap, fdefaults):
        return inmap, fdefaults

    def expand_outmap(self, outmap, fdefaults):
        # Make new command line option (CLO) for that, do not use a keyword
        # from the original function.
        # Syntax like {optname} and in at least one occasion {optname=default}.
        # It is always a string. None means the default values has not been set
        # yet.
        # The string can be a placeholder for any part of a mapping, e.g. a
        # directory, file, group oder dataset name, or a whole part of the
        # path.
        # Example: outmap={0: '#0/{dsetpath=fpar_out}'}
        # So it is always stored in the first output file, under the name given
        # by the new CLO "dsetpath" which defaults to "fpar_out".
        # Implementing this, it should also be allowed to specify a specific
        # file instead of a dynamic definition like $0 or #0 or #@ etc.
        # Then, the whole output filepath/dsetpath can be set by an option,
        # i.e. output={0: '{outpath=out.h5/out}'}
        # Maybe allow to not specifying a default, but because it is a
        # mandatory option then, print an error message if it is omitted.
        return outmap, fdefaults

    def get_tninargs(self, inmap, indep):
        if self.any_startswith(inmap.values(), '$@'):
            return 'inf'
        if not self.any_startswith(inmap.values(), '$'):
            return 0
        if indep:
            return 'inf'
        maxindex = -1
        for mapping in inmap.values():
            index = self.get_argind(mapping, symbol='$')
            if index is not None and index > maxindex:
                maxindex = index
        return maxindex + 1

    def get_tnoutargs(self, outmap, tninargs, indep):
        if self.any_startswith(outmap.values(), '%@/'):
            return 'inf'
        elif self.any_startswith(outmap.values(), '$@/'):
            return 0  # 'same'
        # if indep: return 'inf'  # ???
        maxindex = -1
        for mapping in outmap.values():
            index = self.get_argind(mapping, symbol='%')
            if index is not None and index > maxindex:
                maxindex = index
        return maxindex + 1

    def get_indep(self, inmap, outmap):
        """Determine independent mode, i.e. find out if function only depends
        on maximal one input and maximal one output argument, so that it can be
        applied independently (sequential or parallel) on all the arguments or
        data files.
        """
        # as soon as a $1 (or higher) is found in inmap, or a $1 or %1 is found
        # in outmap, return False
        for mapping in list(inmap.values()) + list(outmap.values()):
            if not isinstance(mapping, str):
                continue
            if mapping.startswith('$@'):
                return False
            if mapping.startswith('%@'):
                return False
            if mapping.startswith('#@'):
                return False
            if mapping.startswith('%') and not '/' in mapping:
                continue
            if mapping.startswith('$') or mapping.startswith('%'):
                slashpos = mapping.find('/')
                slashpos = None if slashpos == -1 else slashpos
                try:
                    value = int(mapping[1:slashpos])
                except:
                    continue
                if value and value > 0:
                    return False
        return True

    def get_exec_mode(self, tninargs, tnoutargs, indep, parallel):
        """Determine execution mode (sequential, parallel or all at once).
        Returns "para", "seq" or "once".
        """
        if tnoutargs in (tninargs, 0) or tninargs == 0:
            if indep:
                return 'para' if parallel else 'seq'
            else:
                if parallel:
                    self.raise_parallel()
                return 'once'
        else:
            if indep:
                self.raise_indep()
            return 'once'

    def divide_args(self, args, tninargs, tnoutargs, indep):
        """Divide arguments into two parts (input and output). If an error is
        found, return the error code. Otherwise, return a tuple of two
        lists.
        """
        nargs = len(args)
        if tninargs == 'inf':
            if tnoutargs == 'inf':
                if not self.is_even(nargs):
                    self.error_arg_pairs()
                mark = nargs / 2
                return args[:mark], args[mark:]
            else:
                if nargs < tnoutargs:
                    return self.error_nargs_min(tnoutargs)
                mark = nargs - tnoutargs
                return args[:mark], args[mark:]
        else:
            if tnoutargs == 'inf':
                if nargs < tninargs:
                    return self.error_nargs_min(tninargs)
                mark = tninargs
                return args[:mark], args[mark:]
            else:
                if not indep and nargs != tninargs+tnoutargs:
                        self.error_nargs(tninargs+tnoutargs)
                mark = tninargs
                return args[:mark], args[mark:]

    @staticmethod
    def get_argind(mapping, symbol='$'):
        """Extract the argument index from a mapping, i.e. the number in "$1"
        or "$2/any_dataset". Return None if mapping is not string, or doesn't
        start with "$", or starts with "$@". Instead of "$", another symbol can
        be chosen.
        """
        if isinstance(mapping, str) and mapping.startswith(symbol):
            slashpos = mapping.find('/')
            slashpos = None if slashpos == -1 else slashpos
            try:
                value = int(mapping[1:slashpos])
            except:
                return None
            return value
        return None

    def display_keywords(self, dictionary):
        print(repr(dictionary))
        #print(repr(dictionary[list(dictionary.keys())[0]][0]))
        #keys = dictionary.keys()
        #keys.sort()
        #for key in keys:
            #value = dictionary[key]
            #print('%s=%s (%s)' % (key, repr(value), type(value).__name__))

    def display_list(self, iterable):
        if iterable:
            #print(', '.join([repr(i) for i in iterable]))
            print(repr(iterable))

    def display_object(self, obj):
        print(repr(obj))

    def split_indata(self, indata):
        """Convert input data (already preprocessed) into a variable list of
        arguments (inargs) and a dictionary of keyword arguments (inkwargs),
        ready to be passed to the function.
        """
        inargs = []
        indata = indata.copy()
        for argname in self.fargnames:
            inargs.append(indata.pop(argname))
        inargs += indata.pop(self.fvarargsname, [])

        inkwargs = indata.pop(self.fvarkwname, {})
        for argname in self.fargnames:
            if argname in inkwargs:
                del inkwargs[argname]
        inkwargs.update(indata)  # put all the remaining arguments in there
        return inargs, inkwargs

    def apply_preproc(self, indata):
        """Apply preprocessors to input data.
        """
        if self.preproc is None:
            return indata
        if not isinstance(self.preproc, dict):
            self.raise_preproc()
        for name, prep in self.preproc.items():
            if prep is not None and name in indata:  # if None,
                                                     # del indata[name]?
                indata[name] = prep(indata[name])
        return indata

    def apply_postproc(self, outdata):
        """Apply postprocessors to output data.
        """
        if hasattr(self.postproc, '__iter__') \
                and not type(self.postproc) is type:
            # if outdata is scalar, also postprocessor must be scalar
            if not hasattr(outdata, '__iter__'):
                self.raise_postproc_iter()
            self.postproc = list(self.postproc)
            while self.postproc and self.postproc[-1] is None:
                del self.postproc[-1]
            if len(self.postproc) > len(outdata):
                self.raise_postproc_len(len(outdata))
            while len(self.postproc) < len(outdata):
                self.postproc.append(None)
            for i in range(len(self.postproc)):
                if self.postproc[i] is None:
                    self.postproc[i] = dummy.function1
            return tuple([postp(outdat)
                          for postp, outdat in zip(self.postproc, outdata)])
        else:
            return self.postproc(outdata)

    def get_from_outdata(self, source, outdata, indata):
        if isinstance(source, str):
            if source == 'ALL':
                data = outdata
            elif source == 'DATE':
                data = time.ctime()
            elif source == 'DATE_START':
                data = self.date_start
            elif source == 'DURATION':
                data = time.time()-self.time0
            elif source == 'TIMINGS':
                data = self.timings
            else:
                # look for an input argument with that name in input mapping
                if source in indata:
                    data = indata[source]
                else:
                    raise KeyError('source "%s" not found in indata' % source)
        else:
            # expect it to be an index referring to output data that has been
            # returned by the function
            try:
                data = outdata[source]
            except IndexError:
                raise IndexError('output data has only length %i'
                                 % len(outdata))
        return data

    def save_outdata_once(self, outargs, outmap, outdata, inargs, indata):
        """Save all output data at once (execution mode "all-at-once").
        """

        # initialize STDOUT datastructure (mapping rowindex-->rowdata or just
        # data. rowdata can in turn be either a mapping colindex-->celldata or
        # just rowdata)
        stdout_data = None

        #if type(outdata) is tuple:
        #    if len(outmap) > len(outdata): self.raise_outmap_len(len(outdata))
        #else:
        #    if len(outmap) > 1: self.raise_outmap_len(1)
        if type(outdata) is not tuple:
            outdata = (outdata,)

        for source, mapping in outmap.items():
            data = self.get_from_outdata(source, outdata, indata)
            if mapping is None:
                continue
            elif isinstance(mapping, str) and mapping.startswith('#'):
                # send data to STDOUT, maybe choose row, maybe choose column
                if mapping.count('/') > 1:
                    self.raise_outmap(mapping)
                if mapping.startswith('#@'):
                    # set whole STDOUT or whole columns at once
                    if not hasattr(data, '__iter__'):
                        self.raise_outdata_iterable(mapping)
                    if '/' in mapping:
                        # set whole column with data
                        if stdout_data is None:
                            stdout_data = {}
                        colindex = int(mapping.split('/')[1])
                        if colindex in stdout_data:
                            self.raise_stdout_structure()
                        for rowindex, item in enumerate(data):
                            if not rowindex in stdout_data:
                                stdout_data[rowindex] = {}
                            if type(stdout_data[rowindex]) is not dict:
                                self.raise_stdout_structure()
                            stdout_data[rowindex][colindex] = data

                    else:
                        # set whole STDOUT with data
                        #if type(stdout_data) is dict:
                            #self.raise_stdout_structure()
                        if stdout_data is None:
                            stdout_data = {}
                        for rowindex, rowdata in enumerate(data):
                            stdout_data[rowindex] = rowdata

                else:
                    # set a whole row or a single cell
                    if stdout_data is None:
                        stdout_data = {}
                    if type(stdout_data) is not dict:
                        self.raise_stdout_structure()
                    rowindex = self.get_argind(mapping, symbol='#')
                    if rowindex is None:
                        self.raise_argind(mapping)
                    if '/' in mapping:
                        # set a specific cell
                        if rowindex not in stdout_data:
                            stdout_data[rowindex] = {}
                        if type(stdout_data[rowindex]) is not dict:
                            self.raise_stdout_structure()
                        colindex = int(mapping.split('/')[1])
                        stdout_data[rowindex][colindex] = data

                    else:
                        # set a whole row
                        stdout_data[rowindex] = str(data)

            elif isinstance(mapping, str) and mapping.startswith('$'):
                # save data back to input file
                if not '/' in mapping:
                    self.raise_outmap(mapping)
                dsetname = '/'.join(mapping.split('/')[1:])
                if mapping.startswith('$@/'):
                    # distribute the (iterable) object to all files
                    # must have the right length
                    if len(inargs) != len(data):
                        self.raise_outdata_len(len(inargs))
                    for fileindex, filename in enumerate(inargs):
                        self.save_dset(filename, dsetname, data[fileindex],
                                       self.overwrite)

                else:
                    # save this object back to one specific input file
                    argind = self.get_argind(mapping, symbol='$')
                    if argind is None:
                        self.raise_argind(mapping)
                    if argind > len(inargs):
                        self.raise_argind(mapping)
                    filename = inargs[argind]
                    self.save_dset(filename, dsetname, data, self.overwrite)

            elif isinstance(mapping, str) and mapping.startswith('%'):
                # save data to dedicated output file
                if not '/' in mapping:
                    self.raise_outmap(mapping)
                dsetname = '/'.join(mapping.split('/')[1:])
                if mapping.startswith('%@/'):
                    # distribute the (iterable) object to all files
                    # must have the right length
                    if len(outargs) != len(data):
                        self.raise_outdata_len(len(outargs))
                    for fileindex, filename in enumerate(outargs):
                        self.save_dset(filename, dsetname, data[fileindex],
                                       self.overwrite)

                else:
                    # save this object to one specific output file
                    argind = self.get_argind(mapping, symbol='%')
                    if argind is None:
                        self.raise_argind(mapping)
                    if argind > len(outargs):
                        self.raise_argind(mapping)
                    filename = outargs[argind]
                    self.save_dset(filename, dsetname, data, self.overwrite)

            else:
                self.raise_outmap(mapping)

        # write to STDOUT
        if stdout_data is None:
            return
        lines = []
        if type(stdout_data) is dict:
            keys = stdout_data.keys()
            nlines = max(keys) + 1 if keys else 0
            lines = ['']*nlines
            for rowindex, row in stdout_data.items():
                if type(row) is dict:
                    rowlen = max(row.keys())+1 if row else 0
                    rowlist = ['']*rowlen
                    for colindex, cell in row.items():
                        rowlist[colindex] = str(cell)
                    line = self.stdout_sep.join(rowlist)
                else:
                    line = str(row)
                lines[rowindex] = line
        else:
            lines = str(stdout_data)
        for line in lines:
            print(line)

    def save_outdata_seq(self, outarg, outmap, outdata, inarg, indata):
        """Save a single chunk of output data (belonging to at most one output
        file or one line of standard output) (execution mode "sequential" or
        "parallel").
        """

        # initialize STDOUT datastructure (mapping colindex-->celldata or
        # just rowdata)
        stdout_data = None

        #if type(outdata) is tuple:
        #    if len(outmap) > len(outdata):
        #        self.raise_outmap_len(len(outdata))
        #else:
        #    if len(outmap) > 1:
        #        self.raise_outmap_len(1)
        if type(outdata) is not tuple:
            outdata = (outdata,)

        for source, mapping in outmap.items():
            data = self.get_from_outdata(source, outdata, indata)
            if mapping is None:
                continue
            elif isinstance(mapping, str) and mapping.startswith('#'):
                # send data to STDOUT, maybe choose column
                if mapping.count('/') > 1:
                    self.raise_outmap(mapping)
                if mapping.startswith('#@'):
                    self.raise_outmap(mapping)
                else:
                    # set a whole row or a single cell
                    if stdout_data is None:
                        stdout_data = {}
                    if type(stdout_data) is not dict:
                        self.raise_stdout_structure()
                    rowindex = self.get_argind(mapping, symbol='#')
                    if rowindex is None or rowindex > 0:
                        self.raise_argind(mapping)
                    if '/' in mapping:
                        # set a specific cell
                        if rowindex not in stdout_data:
                            stdout_data[rowindex] = {}
                        if type(stdout_data[rowindex]) is not dict:
                            self.raise_stdout_structure()
                        colindex = int(mapping.split('/')[1])
                        stdout_data[rowindex][colindex] = data

                    else:
                        # set the whole row
                        stdout_data[rowindex] = data

            elif isinstance(mapping, str) and mapping.startswith('$'):
                # save data back to input file
                if not '/' in mapping:
                    self.raise_outmap(mapping)
                dsetname = '/'.join(mapping.split('/')[1:])
                if mapping.startswith('$@/'):
                    self.raise_outmap(mapping)
                else:
                    # save this object back to one specific input file
                    argind = self.get_argind(mapping, symbol='$')
                    if argind is None or argind > 0:
                        self.raise_argind(mapping)
                    filename = inarg
                    self.save_dset(filename, dsetname, data, self.overwrite)

            elif isinstance(mapping, str) and mapping.startswith('%'):
                # save data to dedicated output file
                if not '/' in mapping:
                    self.raise_outmap(mapping)
                dsetname = '/'.join(mapping.split('/')[1:])
                if mapping.startswith('%@/'):
                    self.raise_outmap(mapping)
                else:
                    # save this object to one specific output file
                    argind = self.get_argind(mapping, symbol='%')
                    if argind is None or argind > 0:
                        self.raise_argind(mapping)
                    filename = outarg
                    self.save_dset(filename, dsetname, data, self.overwrite)

            else:
                self.raise_outmap(mapping)

        # write to STDOUT
        if stdout_data is None:
            return
        elif type(stdout_data) is dict:
            rowlen = max(stdout_data.keys())+1 if stdout_data else 0
            rowlist = ['']*rowlen
            for colindex, cell in stdout_data.items():
                rowlist[colindex] = str(cell)
            line = self.stdout_sep.join(rowlist)
        else:
            line = str(stdout_data)
        if line:
            print(line)

    def save_dset(self, filename, dsetname, data, overwrite=None, mode='a'):
        """Save dataset to file. Overwrite if self.overwrite is True, never
        overwrite if self.overwrite is False, and if it is None, prompt the
        user to decide.
        """
        if os.path.exists(filename):
            with h5py.File(filename, 'r') as f:
                found = dsetname in f
        else:
            found = False
        if found and overwrite is False:
            self.error_dset_omit(dsetname, filename)
        if found and overwrite is None:
            message = '%s: overwrite "%s" [yes|No|all]? ' \
                      % (self.prog, filename+'/'+dsetname)
            answer = raw_input(message).lower()
            if not answer or 'no'.startswith(answer):
                return
            elif 'all'.startswith(answer):
                self.overwrite = True
            elif 'yes'.startswith(answer):
                pass
            else:
                return
        with h5py.File(filename, mode) as f:
            if found:
                del f[dsetname]
            f[dsetname] = data

    def load_indata_once(self, inargs, inmap):
        """Load all input data at once (execution mode "all-at-once").
        """

        # DEFINITION: #0 means "row 1 from stdin", #@ means "all rows from
        # stdin" multiple values from one row are referenced by something like
        # "#0/0", "#0/1", "#0/2"

        # here: load all STDIN at once! do it now!
        # if there are more rows than expected, fine, than just pick the ones
        # referenced (e.g. #0, #2 and #6)
        # if there are not enough rows, then error (e.g. #6 was specified, but
        # STDIN has less than 7 rows)

        if self.any_startswith(inmap.values(), '#'):
                stdin = sys.stdin.readlines()
                stdin_lines = stdin.split('\n') if stdin else []

        indata = {}
        for argname, mapping in inmap.items():
            if isinstance(mapping, str) and mapping.startswith('#'):
                # from STDIN
                if mapping.count('/') > 1:
                    self.raise_inmap(mapping)
                if '/' in mapping:
                    if mapping.startswith == '#@/':
                        # load a specific column
                        data = []
                        colindex = int(mapping.split('/')[1])
                        for line in stdin_lines:
                            values = line.split(self.stdin_sep)
                            if colindex > len(values)-1:
                                self.raise_stdin_structure()
                            data.append(values[colindex].strip())
                        indata[argname] = data
                    else:
                        # load a specific cell
                        argind = self.get_argind(mapping, symbol='#')
                        if argind is None:
                            self.raise_argind(mapping)
                        if argind > len(stdin_lines):
                            self.error_stdin_len()
                        values = stdin_lines[argind].split(self.stdin_sep)
                        colindex = int(mapping.split('/')[1])
                        if colindex > len(values)-1:
                            self.raise_stdin_structure()
                        indata[argname] = values[colindex].strip()
                else:
                    if mapping == '#@':
                        # load whole standard input
                        indata[argname] = stdin
                    else:
                        # load a specific row
                        argind = self.get_argind(mapping, symbol='#')
                        if argind is None:
                            self.raise_argind(mapping)
                        if argind > len(stdin_lines):
                            self.error_stdin_len()
                        indata[argname] = stdin_lines[argind]

            elif isinstance(mapping, str) and mapping.startswith('$'):
                # from CL argument
                if '/' in mapping:
                    # CL argument is filename, load data from file
                    dsetname = '/'.join(mapping.split('/')[1:])
                    if mapping.startswith('$@/'):
                        # get dataset from all files
                        indata[argname] = []
                        for filename in inargs:
                            with h5py.File(filename, 'r') as f:
                                found = dsetname in f
                                if found:
                                    data = f[dsetname][()]
                            if not found:
                                #if argname in self.fdefaults: ### wrong here
                                #  data = self.fdefaults[argname]
                                #else:
                                if self.omit_missing:
                                    continue
                                else:
                                    self.error_dset_not_found(dsetname, filename)
                            indata[argname].append(data)
                    else:
                        # get dataset from one specific file
                        argind = self.get_argind(mapping, symbol='$')
                        if argind is None:
                            self.raise_argind(mapping)
                        filename = inargs[argind]
                        if not os.path.exists(filename):
                            self.error_file(filename)
                        with h5py.File(filename, 'r') as f:
                            found = dsetname in f
                            if found:
                                data = f[dsetname]
                        if not found:
                            if argname in self.fdefaults:
                                data = self.fdefaults[argname]
                            else:
                                self.error_dset_not_found(dsetname, filename)
                        indata[argname] = data
                else:
                    # CL argument contains data itself
                    if mapping == '$@':
                        indata[argname] = inargs
                    else:
                        argind = self.get_argind(mapping, symbol='$')
                        indata[argname] = inargs[argind]

            elif isinstance(mapping, str) and mapping.startswith('%'):
                self.raise_percent_in_inmap(mapping)

            else:
                # set default value for this argument, make it an option
                # really neccessary to add it to options here? I don't think
                # so check inmap earlier, so that an option is created
                #self.fdefaults[argname] = mapping
                #if argname in self.frequired:
                    #del self.frequired.index(argname)
                indata[argname] = mapping

        return indata

    def load_indata_seq(self, inarg, inmap):
        """Load only one chunk of data (belonging to one command line argument,
        one input file, or one line of standard input) (execution mode
        "sequential" or "parallel").
        """

        if self.any_startswith(inmap.values(), '#'):
            stdin_line = self.read_stdin_line()
            if self._stdin_eof:
                return {}  # leave early, cancel function call

        indata = {}
        for argname, mapping in inmap.items():
            if isinstance(mapping, str) and mapping.startswith('#'):
                # from STDIN
                if mapping == '#@':
                    self.raise_inmap(mapping)
                if mapping.count('/') > 1:
                    self.raise_inmap(mapping)
                argind = self.get_argind(mapping, symbol='#')
                if argind is None or argind > 0:
                    self.raise_argind(mapping)
                if '/' in mapping:
                    # load specific cell
                    values = stdin_line.split(self.stdin_sep)
                    colindex = int(mapping.split('/')[1])
                    if colindex > len(values)-1:
                        self.raise_stdin_structure()
                    indata[argname] = values[colindex].strip()
                else:
                    # load whole row
                    indata[argname] = stdin_line.strip()
            elif isinstance(mapping, str) and mapping.startswith('$'):
                # from CL argument
                if '/' in mapping:
                    # CL argument is filename, load data from file
                    dsetname = '/'.join(mapping.split('/')[1:])
                    if mapping.startswith('$@/'):
                        self.raise_inmap(mapping)
                    else:
                        # get dataset from one specific file
                        argind = self.get_argind(mapping, symbol='$')
                        if argind is None or argind > 0:
                            self.raise_argind(mapping)
                        filename = inarg
                        if not os.path.exists(filename):
                            self.error_file(filename)
                        with h5py.File(filename, 'r') as f:
                            found = dsetname in f
                            if found:
                                data = f[dsetname]
                        if not found:
                            if argname in self.fdefaults:
                                data = self.fdefaults[argname]
                            else:
                                ### allow omitting files here as well
                                self.error_dset_not_found(dsetname, filename)
                        indata[argname] = data
                else:
                    # CL argument contains data itself
                    if mapping == '$@':
                        self.raise_inmap(mapping)
                    else:
                        argind = self.get_argind(mapping, symbol='$')
                        if argind > 0:
                            self.raise_argind(mapping)
                        indata[argname] = inarg

            elif isinstance(mapping, str) and mapping.startswith('%'):
                self.raise_percent_in_inmap(mapping)

            else:
                # set default value for this argument, make it an option
                # really neccessary to add it to options here? I don't think so
                # check inmap earlier, so that an option is created
                #self.fdefaults[argname] = mapping
                #if argname in self.frequired:
                    #del self.frequired.index(argname)
                indata[argname] = mapping

        return indata

    def read_stdin_line(self):
        try:
            line = sys.stdin.readline()
        except KeyboardInterrupt:  # "CTRL-c pressed"
            self._stdin_eof = True
            line = ','*(len(self.frequired)-1)
        if not line:
            self._stdin_eof = True  # "CTRL-d pressed" / EOF
            line = ','*(len(self.frequired)-1)
        # strip just one '\n' at the end of the lines, not all
        if len(line) > 0 and line[-1] == '\n':
            line = line[:-1]
        return line

    @staticmethod
    def any_startswith(objects, string):
        """Return true if any of the given objects starts with the given
        string. Non-strings among the objects are ignored.
        """
        for obj in objects:
            if not isinstance(obj, str):
                continue
            if obj.startswith(string):
                return True
        return False

    @staticmethod
    def in_any(obj, iterable):
        for item in iterable:
            if obj in item:
                return True
        return False

    @staticmethod
    def startswith_one_of(string, chars):
        return string and string[0] in chars

    @staticmethod
    def raise_outdata_iterable(mapping):
        raise ValueError('expecting iterable output data for mapping "%s"'
                         % mapping)

    @staticmethod
    def raise_percent_in_inmap(mapping):
        raise ValueError('bad input mapping: "%s"' % mapping)

    @staticmethod
    def raise_exec_mode():
        raise ValueError('expecting "once", "seq" or "para"')

    @staticmethod
    def raise_parallel():
        raise ValueError('parallel execution not possible if not independent')

    @staticmethod
    def raise_indep():
        raise ValueError('independent execution not possible if ' +
                         'infiles != outfiles')

    @staticmethod
    def raise_outdata(outlen):
        raise ValueError('expecting function result with at least ' +
                         '%i elements' % outlen)

    @staticmethod
    def raise_postproc_iter():
        raise ValueError('postprocessor must be scalar if output data is ' +
                         'scalar')

    @staticmethod
    def raise_postproc_len(outlen):
        raise ValueError('list of postprocessors cannot be longer than ' +
                         'function result (%i)' % outlen)

    @staticmethod
    def raise_preproc():
        raise ValueError('expecting dictionary of callables')

    @staticmethod
    def raise_reqarg_not_in_inmap(reqarg):
        raise ValueError('argument "%s" required but missing in input ' +
                         'mapping' % reqarg)

    def raise_outmap_len(self, noutdata):
        raise ValueError('output mapping too long (function delivered only ' +
                         '%i output value%s)'
                         % (noutdata, self.plural(noutdata)))

    @staticmethod
    def raise_argind(mapping):
        raise ValueError('bad argument index in mapping "%s"' % mapping)

    @staticmethod
    def raise_outdata_len(nexpected):
        raise ValueError('expected function return value of length %i'
                         % nexpected)

    @staticmethod
    def raise_inmap(mapping):
        raise ValueError('bad input mapping "%s"' % mapping)

    @staticmethod
    def raise_outmap(mapping):
        raise ValueError('bad output mapping "%s"' % mapping)

    @staticmethod
    def raise_stdin_structure():
        raise ValueError('bad STDIN data structure')

    @staticmethod
    def raise_stdout_structure():
        raise ValueError('bad STDOUT data structure')

    def error_pairs(self):
        print(f'{self.prog}: expecting even number of arguments', file=sys.stderr)
        sys.exit(1)

    def error_arg_pairs(self):
        print(f'{self.prog}: expecting same number of output arguments',
              'as number of input arguments', file=sys.stderr)
        sys.exit(1)

    def error_nargs(self, nargs):
        print(f'{self.prog}: expecting exactly {nargs} argument{self.plural(nargs)}',
              file=sys.stderr)
        sys.exit(1)

    def error_nargs_min(self, nargs):
        print(f'{self.prog}: expecting at least {nargs} argument{self.plural(nargs)}',
              file=sys.stderr)
        sys.exit(1)

    def error_file(self, filename):
        print(f'{self.prog}: cannot load from "{filename}": no such file', file=sys.stderr)
        sys.exit(1)

    def error_dset_not_found(self, dsetname, filename):
        print(f'{self.prog}: cannot load dataset "{filename}/{dsetname}": no such dataset',
              file=sys.stderr)
        sys.exit(1)

    def error_dset_exists(self, dsetname, filename):
        print(f'{self.prog}: dataset already exists: {filename}/{dsetname}',
              file=sys.stderr)
        sys.exit(1)

    def error_dset_omit(self, dsetname, filename):
        print(f'{self.prog}: omitting dataset "{filename}/{dsetname}": already exists',
              file=sys.stderr)

    def error_shortopt(self, shortopt):
        print(f'{self.prog}: invalid short option "{shortopt}"', file=sys.stderr)
        sys.exit(1)

    def error_stdin_len(self, nlines):
        print(f'{self.prog}: expecting at least {nlines} line{self.plural(nlines)} of STDIN',
              file=sys.stderr)
        sys.exit(1)

    #def error_arg_len(self, inlen):
        #print(f'{self.prog}: expecting commandline argument with {inlen}',
        #      f'element{self.plural(inlen)} (separated by "{self.sep_stdin}")',
        #      file=sys.stderr)
        #sys.exit(1)

    @staticmethod
    def plural(number):
        return '' if abs(number) == 1 else 's'

    @staticmethod
    def is_even(number):
        return not number % 2


#=======================================================#
# tools to list comliners and create executable scripts #
#=======================================================#


@Comliner(postproc=columnize)
def comlinerlist(module):
    """List all comliners defined in a certain module. The module can be a
    string (module path) or the module object itself.
    """
    if isinstance(module, str):
        module = __import__(module, fromlist=['something'])
        # it is an interesting issue why "fromlist" cannot be an empty list if
        # __import__ shall return the rightmost submodule in a module path:
        # http://stackoverflow.com/questions/2724260/why-does-pythons-import
        # -require-fromlist
    names = []
    for name, obj in module.__dict__.items():
        if iscomliner(obj):
            names.append(name)
    names.sort()
    return names


@Comliner()
def comlinerexec(comliner, name=None, dir='.'):
    """Create a small executable script that calls the specified comliner
    wrapper. Expect either the function object of the comliner wrapper itself,
    or a string containing the full module path to that comliner wrapper.

    The executable script is created in the directory "dir". Default is the
    current working directory. The script will be named according to "name",
    otherwise it will be based on the name of the function object of the
    comliner wrapper.

    An attempt is made to make the script executable, using "chmod +x".
    """
    if isinstance(comliner, str):
        # decode module path
        modulename, comlinername = comliner.rsplit('.', 1)
        #modulename_orig = modulename
        module = None
        others = []
        while True:
            try:
                module = __import__(modulename, fromlist=['something'])
            except:
                modulename, other = modulename.rsplit('.', 1)
                others.append(other)
                continue
            break
        if module is None:
            raise ImportError('unable to locate comliner definition %s' % comliner)
        module2 = module
        for other in others:
            module2 = getattr(module2, other)

        comliner = getattr(module2, comlinername)
    else:
        # get name of module and name of comliner wrapper
        modulename, comlinername = __name__, comliner.__name__
    #print(modulename, others, comlinername)

    # check object
    if not iscomliner(comliner):
        raise ValueError('given function object is not a comliner')

    # determine filename
    if not name:
        name = comliner.__name__
        while name.startswith('_'):
            name = name[1:]
    path = os.path.relpath(os.path.join(dir, name))
    if os.path.exists(path):
        raise IOError('file exists: %s' % path)

    # create the file
    with open(path, 'w') as f:
        f.write('#!/usr/bin/env python\n')
        f.write('# -*- coding: utf-8 -*-\n')
        f.write('import sys\n')
        f.write('import %s\n' % modulename)
        otherspath = '.'.join(others)
        if otherspath:
            otherspath += '.'
        f.write('comliner = %s.%s%s\n' % (modulename, otherspath, comlinername))
        #f.write('from %s import %s as comliner\n' % (modulename, comlinername))
        f.write('sys.exit(comliner())\n')

    # try to make it executable
    try:
        os.system('chmod +x %s' % path)
    except:
        print(f'warning: unable to change permissions: {path}', file=sys.stderr)


def iscomliner(func):
    """Check if the given function object possesses at least one comliner
    wrapper.

    Background: As soon as a comliner decorator is applied to a function, it leaves
    a trace by adding an attribute called *__comliner__* to the function."""
    return hasattr(func, '__comliner__') and func.__comliner__


@Comliner(inmap=dict(timings='$0/timings'))
def print_timings(timings):
    string = ''
    string += 'total: %g | prepare: %g | first: %g | last: %g\n' \
        % (timings.get('total', 0),
           timings.get('prepare', 0),
           timings.get('first', 0),
           timings.get('last', 0))

    tabdict = {}
    for field in ('loop', 'load', 'preproc', 'call', 'postproc', 'save'):
        data = timings.get(field, [])
        if not data:
            continue
        mean = sum(data)/len(data)
        fielddict = {}
        fielddict['mean'] = mean
        fielddict['min'] = min(data)
        fielddict['max'] = max(data)
        tabdict[field] = fielddict
    string += clitable.autotable(tabdict, titles=True)
    return string


# still with _nicetime
#@Comliner(inmap=dict(timings='$0/timings'))
#def print_timings(timings):
#    string = ''
#    string += 'total: %s | prepare: %s | first: %s | last: %s\n' \
#        % (_nicetime(timings.get('total', 0)),
#            _nicetime(timings.get('prepare', 0)),
#            _nicetime(timings.get('first', 0)),
#            _nicetime(timings.get('last', 0)))
#
#    string += '          mean      min       max\n'
#    for field in ('loop', 'load', 'preproc', 'call', 'postproc', 'save'):
#        data = timings.get(field, [])
#        if not data:
#            continue
#        mean = sum(data)/len(data)
#        dmin = min(data)
#        dmax = max(data)
#        string += '%-8s  %-8s  %-8s  %-8s\n' \
#                % (field, _nicetime(mean), _nicetime(dmin),
#                        _nicetime(dmax))
#    return string


#===========================================================#
# convenience functions and classes for datatype conversion #
#===========================================================#


class list_of(object):
    """Instances of this class are callables which turn a given iterable into a
    list of items with the specified data type.
    """

    def __init__(self, dtype):
        self.dtype = dtype

    def __call__(self, iterable):
        #print(repr(iterable))
        iterable = list(iterable)
        #print(repr(iterable))
        for i in range(len(iterable)):
            #print(repr(iterable[i]))
            iterable[i] = self.dtype(iterable[i])
        return iterable


class tuple_of(object):
    """Instances of this class are callables which turn a given iterable into a
    tuple of items with the specified data type.
    """

    def __init__(self, dtype):
        self.dtype = dtype

    def __call__(self, iterable):
        iterable = list(iterable)
        for i in range(len(iterable)):
            iterable[i] = self.dtype(iterable[i])
        return tuple(iterable)


def sentence(iterable):
    """Convert all items of the iterable to strings and join them with space
    characters in between. Return the newly formed string.
    """
    return ' '.join(str(i) for i in iterable)


class apply_all(object):
    """Instances of this class are callables which apply a list of functions to
    the given object.
    """

    def __init__(self, *funcs):
        self.funcs = funcs

    def __call__(self, obj):
        for func in self.funcs:
            obj = func(obj)
        return obj


def eval_if_str(obj):
    """Evaluate given expression only if string is given, otherwise, leave the
    given object unchanged.
    """
    return eval(obj) if isinstance(obj, str) else obj


class items_of(object):
    """Instances of this class are callables which get a certain item of each
    element of a given iterable, and returns all items in form of a new
    iterable. If item does not exist and a default value is given, return that
    value.
    """

    def __init__(self, itemname, default=None, dtype=None):
        self.itemname = itemname
        self.default = default
        self.dtype = dtype

    def __call__(self, iterable):
        dtype = self.dtype or type(iterable)
        newiter = []
        for item in iterable:
            if self.default is not None:
                try:
                    value = item[self.itemname]
                except:
                    value = self.default
            else:
                value = item[self.itemname]
            newiter.append(value)
        return dtype(newiter)


class expressions_of(object):
    """Instances of this class are callables which evaluate a certain
    expression for each element of a given iterable, and returns the results as
    in form of a new iterable. In the expression, "x" indicates the respective
    item.
    """

    def __init__(self, expr='x', dtype=None):
        self.expr = expr
        self.dtype = dtype

    def __call__(self, iterable):
        dtype = self.dtype or type(iterable)
        newiter = []
        for item in iterable:
            res = eval(self.expr, dict(x=item))
            newiter.append(res)
        return dtype(newiter)


#def _nicetime(seconds):
#  """Return nice string representation of the given number of seconds in a
#  human-readable format (approximated). Example: 3634 s --> 1 h.
# """
#  # 2013-08-06
#  # copied from progress._nicetime (written 2012-09-04)
#  # copied from tb.misc.nicetime (written 2012-02-17)
#  from itertools import izip
#
#  # create list of time units (must be sorted from small to large units)
#  units = [{'factor': 1,  'name': 'sec'},
#           {'factor': 60, 'name': 'min'},
#           {'factor': 60, 'name': 'hrs'},
#           {'factor': 24, 'name': 'dys'},
#           {'factor': 7,  'name': 'wks'},
#           {'factor': 4,  'name': 'mns'},
#           {'factor': 12, 'name': 'yrs'}]
#
#  value = int(seconds)
#  for unit1, unit2 in izip(units[:-1], units[1:]):
#    if value/unit2['factor'] == 0:
#      return '%i %s' % (value, unit1['name'])
#    else:
#      value /= unit2['factor']
#  return '%i %s' % (value, unit2['name'])
