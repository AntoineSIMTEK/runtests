from .coverage import Coverage
from . import conftest

import pytest
import traceback
import sys
import os
import contextlib
import shutil
import subprocess
import time

def _make_clean_dir(path):
    try:
        shutil.rmtree(path)
    except OSError:
        pass
    try:
        os.makedirs(path)
    except OSError:
        pass

class Tester(object):
    """
    Run tests using pytest, building a fresh version of the project first.
    
    Examples::
        $ python runtests.py my/module 
        $ python runtests.py my/module/tests/test_abc.py
        $ python runtests.py
    """
    plugins = [conftest.build]
    
    def __init__(self, package_file, module,
            extra_path =['/usr/lib/ccache', '/usr/lib/f90cache',
                         '/usr/local/lib/ccache', '/usr/local/lib/f90cache']
        ):
        """
        Parameters
        ----------
        package_files : str
            the path to the main directory of the source package
        module : str
            the name of the package to test
        extra path : list of str
            extra paths to include on PATH when building
        """
        self.ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(package_file)))
        self.PROJECT_ROOT_FILES = [package_file, 'setup.py']
        self.EXTRA_PATH = extra_path
        self.PROJECT_MODULE = module
        self.TEST_DIR = os.path.join(self.ROOT_DIR, 'build', 'test')
        self.DEST_DIR = os.path.join(self.ROOT_DIR, 'build', 'testenv')

        from distutils.sysconfig import get_python_lib
        site_dir = get_python_lib(prefix=self.DEST_DIR, plat_specific=True)
        site_dir_noarch = get_python_lib(prefix=self.DEST_DIR, plat_specific=False)

        self.SITE_DIRS = [site_dir, site_dir_noarch]

    def main(self, argv):
        """
        The main function to run the tests
        
        Parameters
        ----------
        argv : list of str
            the command-line arguments -- should be equal to ``sys.argv[1:]``
        """
        # initialize the pytest configuration from the command-line args
        config = self._get_pytest_config(argv)
        args = config.known_args_namespace

        # make the test directory exists
        self._initialize_dirs()

        # print help and exit
        if args.help:
            return config.hook.pytest_cmdline_main(config=config)

        # import project from system path
        # this forces pytest to import the freshly built package
        args.pyargs = True

        # build the project, returning the site directory
        site_dir = self._do_build(args)

        if args.shell:
            self._do_shell(args, config)

        if args.build_only:
            sys.exit(0)

        # fix the path of the modules we are testing
        # so they point to site_dir
        config.args = self._fix_test_paths(site_dir, config.args) 

        # extract the coverage-related options
        covargs = {}
        covargs['with_coverage'] = args.with_coverage
        covargs['config_file'] = args.cov_config
        covargs['html_cov'] = args.html_cov

        # run the tests
        try:
            code = None
            with self._run_from_testdir(args):
                code = self._test(config, **covargs)
        except:
            traceback.print_exc()
            sys.exit(1)

        sys.exit(code)

    def _test(self, config, **kwargs):
        """
        Run the actual tests with optional coverage -- a wrapper around 
        the pytest calling sequence
        
        Parameters
        ----------
        config : 
            the pytest configuration object
        kwargs : 
            additional keywords to pass to the Coverage class
        """
        try:
            with Coverage(self.PROJECT_MODULE, root=self.ROOT_DIR, **kwargs):
                config.pluginmanager.check_pending()
                return config.hook.pytest_cmdline_main(config=config)
        finally:
            config._ensure_unconfigure()
            
    def _do_build(self, args):
        """
        Build the project and return the site directory in the 
        build/ directory
        """
        if not args.no_build:
            self._build_project(args)

        for site_dir in self.SITE_DIRS:
            if os.path.exists(os.path.join(site_dir, self.PROJECT_MODULE)):
                break
        else:
            print("Package %s not properly installed" % self.PROJECT_MODULE)
            sys.exit(1)

        sys.path.insert(0, site_dir)
        os.environ['PYTHONPATH'] = site_dir

        return site_dir

    def _do_shell(self, args, config):
        capman = config.pluginmanager.getplugin('capturemanager')
        if capman:
            out, err = capman.suspendcapture(in_=True)
        shell = os.environ.get('SHELL', 'sh')
        print("Spawning a Unix shell...")
        with self._run_from_testdir(args):
            os.execv(shell, [shell] + config.args[1:])
            sys.exit(1)

    @contextlib.contextmanager
    def _run_from_testdir(self, args):
        """
        Context manager to safely change directory
        to ``build/test``
        """
        cwd = os.getcwd()

        try:
            os.chdir(self.TEST_DIR)
            yield
        finally:
            os.chdir(cwd)
    
    def _get_pytest_config(self, argv):
        """
        Return the ``pytest`` configuration object based on the 
        command-line arguments
        """
        import _pytest.config as _config
        
        # disable pytest-cov
        argv += ['-p', 'no:pytest_cov']
        
        # get the pytest configuration object
        try:
            config = _config._prepareconfig(argv, self.plugins)
        except _config.ConftestImportFailure as e:
            tw = _config.py.io.TerminalWriter(sys.stderr)
            for line in traceback.format_exception(*e.excinfo):
                tw.line(line.rstrip(), red=True)
            tw.line("ERROR: could not load %s\n" % (e.path), red=True)
            raise 
            
        return config
    
    def _initialize_dirs(self):
        """
        Initialize the ``build/test/`` directory
        """
        _make_clean_dir(self.TEST_DIR)
        _make_clean_dir(self.DEST_DIR)

    def _fix_test_paths(self, site_dir, args):
        """
        Fix the paths of tests to run to point to the corresponding
        tests in the site directory
        """
        def fix_test_path(x):
            p = x.split('::')
            p[0] = os.path.relpath(os.path.abspath(p[0]), self.ROOT_DIR)
            p[0] = os.path.join(site_dir, p[0])
            return '::'.join(p)
        return [fix_test_path(x) for x in args]

    def _build_project(self, args):
        """
        Build a dev version of the project.

        Returns
        -------
        site_dir
            site-packages directory where it was installed
        """

        root_ok = [os.path.exists(os.path.join(self.ROOT_DIR, fn))
                   for fn in self.PROJECT_ROOT_FILES]

        if not all(root_ok):
            print("To build the project, run runtests.py in "
                  "git checkout or unpacked source")
            sys.exit(1)

        env = dict(os.environ)
        cmd = [sys.executable, 'setup.py']

        # Always use ccache, if installed
        env['PATH'] = os.pathsep.join(self.EXTRA_PATH + env.get('PATH', '').split(os.pathsep))

        # easy_install won't install to a path that Python by default cannot see
        # and isn't on the PYTHONPATH.  Plus, it has to exist.
        for dir in self.SITE_DIRS:
            basedir = os.path.dirname(dir)
            if not os.path.exists(basedir):
                os.makedirs(basedir)

        env['PYTHONPATH'] = ':'.join(self.SITE_DIRS)

        if args.debug:
            # assume everyone who debugs uses gcc/gfortran
            env['OPT'] = '-O0 -ggdb'
            env['FOPT'] = '-O0 -ggdb'

        cmd += ['build']

        if args.parallel > 1:
            cmd += ["-j", str(args.parallel)]

        with open('setup.py', 'rt') as ff:
            text = ff.read()
            import re
            if re.search('from\s\s*setuptools', text):
                use_setuptools = True
            else:
                use_setuptools = False

        if use_setuptools:
            cmd += ['install', '--prefix=' + self.DEST_DIR,
                    '--single-version-externally-managed',
                    '--record=' + self.DEST_DIR + 'tmp_install_log.txt']
        else:
            cmd += ['install', '--prefix=' + self.DEST_DIR]


        if args.show_build_log:
            ret = subprocess.call(cmd, env=env, cwd=self.ROOT_DIR)
        else:
            log_filename = os.path.join(self.ROOT_DIR, 'build.log')
            print("Building, see build.log...")
            with open(log_filename, 'w') as log:
                p = subprocess.Popen(cmd, env=env, stdout=log, stderr=log,
                                     cwd=self.ROOT_DIR)

        # Wait for it to finish, and print something to indicate the
        # process is alive, but only if the log file has grown (to
        # allow continuous integration environments kill a hanging
        # process accurately if it produces no output)
        last_blip = time.time()
        last_log_size = os.stat(log_filename).st_size
        while p.poll() is None:
            time.sleep(0.5)
            if time.time() - last_blip > 60:
                log_size = os.stat(log_filename).st_size
                if log_size > last_log_size:
                    print("    ... build in progress")
                    last_blip = time.time()
                    last_log_size = log_size

            ret = p.wait()

        if ret == 0:
            print("Build OK")
        else:
            if not args.show_build_log:
                with open(log_filename, 'r') as f:
                    print(f.read())
                print("Build failed!")
            sys.exit(1)

