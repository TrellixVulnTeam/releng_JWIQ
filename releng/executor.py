"""
Provides mockable object for all operations that affect the external state

The Executor object is used in Jenkins builds, but a DryRunExecutor can be
dropped into its place for local testing, and unit tests can use a mock
Executor object instead.

All operations that interact with the world outside the releng script (such as
the file system or external commands) should be wrapped within the Executor
object to allow the above replacements to work as intended.  For now, this is
not used throughout the scripts, but its use and scope will be extended with
expanding unit tests.
"""
from __future__ import print_function

from distutils.spawn import find_executable
import os
import pipes
import re
import shutil
import subprocess
import sys

from common import CommandError, System
import utils

def _ensure_abs_path(path, cwd):
    if not os.path.isabs(path):
        path = os.path.join(cwd, path)
    return path

def _read_file(path, binary):
    if binary:
        with open(path, 'rb') as fp:
            for block in iter(lambda: fp.read(4096), b''):
                yield block
    else:
        with open(path, 'r') as fp:
            for line in fp:
                yield line

class Executor(object):
    """Real executor for Jenkins builds that does all operations for real."""

    def __init__(self, factory):
        self._cwd = factory.cwd

    @property
    def console(self):
        return sys.stdout

    def exit(self, exitcode):
        sys.exit(exitcode)

    def call(self, cmd, **kwargs):
        return subprocess.call(cmd, **kwargs)

    def check_call(self, cmd, **kwargs):
        subprocess.check_call(cmd, **kwargs)

    def check_output(self, cmd, **kwargs):
        return subprocess.check_output(cmd, **kwargs)

    def remove_path(self, path):
        """Deletes a file or a directory at a given path if it exists."""
        path = _ensure_abs_path(path, self._cwd.cwd)
        if os.path.isdir(path):
            shutil.rmtree(path)
        elif os.path.exists(path):
            os.remove(path)

    def ensure_dir_exists(self, path, ensure_empty=False):
        """Ensures that a directory exists and optionally that it is empty."""
        path = _ensure_abs_path(path, self._cwd.cwd)
        if ensure_empty:
            self.remove_path(path)
        elif os.path.isdir(path):
            return
        os.makedirs(path)

    def read_file(self, path, binary=False):
        """Iterates over lines in a file."""
        path = _ensure_abs_path(path, self._cwd.cwd)
        return _read_file(path, binary)

    def write_file(self, path, contents):
        """Writes a file with the given contents."""
        path = _ensure_abs_path(path, self._cwd.cwd)
        with open(path, 'w') as fp:
            fp.write(contents)

class DryRunExecutor(object):
    """Executor replacement for manual testing dry runs."""

    def __init__(self, factory):
        self._cwd = factory.cwd

    @property
    def console(self):
        return sys.stdout

    def exit(self, exitcode):
        sys.exit(exitcode)

    def call(self, cmd, **kwargs):
        return 0

    def check_call(self, cmd, **kwargs):
        pass

    def check_output(self, cmd, **kwargs):
        pass

    def remove_path(self, path):
        print('delete: ' + path)

    def ensure_dir_exists(self, path, ensure_empty=False):
        pass

    def read_file(self, path, binary=False):
        path = _ensure_abs_path(path, self._cwd.cwd)
        return _read_file(path, binary)

    def write_file(self, path, contents):
        print('write: ' + path + ' <<<')
        print(contents + '<<<')

class CurrentDirectoryTracker(object):
    """Helper class for tracking the current directory for command execution."""

    def __init__(self):
        self.cwd = os.getcwd()

    def chdir(self, path):
        assert os.path.isabs(path)
        self.cwd = path

class CommandRunner(object):

    def __init__(self, factory):
        self._cwd = factory.cwd
        self._env = dict(factory.env)
        self._shell_call_opts = dict()
        if factory.system and factory.system != System.WINDOWS:
            self._shell_call_opts['executable'] = '/bin/bash'
        self._is_windows = factory.system and factory.system == System.WINDOWS
        self._executor = factory.executor

    def set_env_var(self, variable, value):
        if value is not None:
            self._env[variable] = value

    def append_to_env_var(self, variable, value, sep=' '):
        if variable in self._env and self._env[variable]:
            self._env[variable] += sep + value
        else:
            self._env[variable] = value

    def prepend_to_env_var(self, variable, value, sep=' '):
        if variable in self._env and self._env[variable]:
            self._env[variable] = sep.join((value, self._env[variable]))
        else:
            self._env[variable] = value

    def import_env(self, env_cmd, cmake_command):
        cmd = env_cmd + ' && {0} -E environment'.format(cmake_command)
        new_env = self.check_output(cmd, shell=True)
        if new_env:
            for line in new_env.splitlines():
                if re.match(r'\w+=', line):
                    variable, value = line.strip().split('=', 1)
                    self._env[variable] = value
                else:
                    print(line, file=self._executor.console)

    def call(self, cmd, **kwargs):
        cmd_string, kwargs = self._prepare_cmd(cmd, kwargs)
        try:
            return self._executor.call(cmd, **kwargs)
        except subprocess.CalledProcessError:
            raise CommandError(cmd_string)

    def check_call(self, cmd, **kwargs):
        cmd_string, kwargs = self._prepare_cmd(cmd, kwargs)
        try:
            self._executor.check_call(cmd, **kwargs)
        except subprocess.CalledProcessError:
            raise CommandError(cmd_string)

    def check_output(self, cmd, **kwargs):
        cmd_string, kwargs = self._prepare_cmd(cmd, kwargs)
        try:
            return self._executor.check_output(cmd, **kwargs)
        except subprocess.CalledProcessError:
            raise CommandError(cmd_string)

    def _prepare_cmd(self, cmd, kwargs):
        shell = kwargs.get('shell', False)
        cmd_string = self._cmd_to_string(cmd, shell)
        print('+ ' + cmd_string, file=self._executor.console)
        if shell:
            kwargs.update(self._shell_call_opts)
        if not 'cwd' in kwargs:
            kwargs['cwd'] = self._cwd.cwd
        if not 'env' in kwargs:
            kwargs['env'] = self._env
        utils.flush_output()
        return cmd_string, kwargs

    def _cmd_to_string(self, cmd, shell):
        """Converts a shell command from a string/list into properly escaped string."""
        if shell:
            return cmd
        elif self._is_windows:
            return subprocess.list2cmdline(cmd)
        else:
            return ' '.join([pipes.quote(x) for x in cmd])

    def find_executable(self, name):
        """Returns the full path to the given executable."""
        # If we at some point require Python 3.3, shutil.which() would be
        # more obvious.
        return find_executable(name, path=self._env['PATH'])
