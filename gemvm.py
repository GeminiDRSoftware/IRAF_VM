#!/usr/bin/env python3
#
# Copyright(c) 2022 Association of Universities for Research in Astronomy, Inc.

import asyncio
import contextlib
import functools
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
import traceback

ssh_port = 2222
mem_GB = 3


class VMControl:

    states = ('off', 'booting', 'running', 'shutting_down')

    def __init__(self, disk_image, cmd='qemu-system-x86_64', mem=3, port=2222,
                 flush_log=False):

        self.disk_image = disk_image
        self.cmd = cmd
        self.mem = mem
        self.port = port
        self.flush_log = flush_log

        self.title, _ = os.path.splitext(os.path.basename(self.disk_image))
        self.log_file = f'gemvm_{self.title}.log'
        self.qmp_sock = os.path.join(os.sep, 'tmp', f'.gemvm_qmp_{os.getpid()}')

        self._state = 'off'
        self._qmp_established = False
        self._tasks = {}
        self._log_fd = None

        self.pid = None
        self.exit_status = None
        self.mem_err = False

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, state):
        if state in self.states:
            self._state = state
        else:
            raise ValueError(f'Invalid VM state: {state}')

    @property
    def qmp_established(self):
        return self._qmp_established

    @property
    def cmd_args(self):

        return (
            f'-m {self.mem}G',
            f'-hda {self.disk_image}',
            # f'-drive file={self.disk_image},if=virtio,cache=off',
            f'-name "{self.title}"',
            f'-machine q35',
            f'-smp 2',
            f'-vga none',
            f'-nographic',
            f'-boot menu=off',
            f'-qmp unix:{self.qmp_sock},server,nowait',
            f'-device e1000,netdev=net0',
            # f'-device virtio-net-pci,netdev=net0',
            f'-netdev user,id=net0,hostfwd=tcp:127.0.0.1:{self.port}-:22',
        )

    def log_context(self):
        if self._log_fd and not self._log_fd.closed:
            # If the log's already/still open, re-use file handle:
            return contextlib.nullcontext(self._log_fd)
        else:
            # Hold log file open for duration of outermost context manager
            # (just a single write if that's the context in self.log):
            self._log_fd = open(self.log_file, 'a+',
                                encoding='utf-8', errors='surrogateescape')
            return self._log_fd

    def log(self, msg, time_stamp=True):
        with self.log_context() as log_fd:
            if time_stamp:
                ts = time.strftime("%H:%M:%S", time.localtime())
                log_fd.write(f'{ts}  {msg}\n')
            else:
                log_fd.write(f'{msg}\n')
            if self.flush_log:
                log_fd.flush()

    # Only one instance can be called from a given Python process without a QMP
    # socket conflict. This call blocks execution anyway but isn't thread safe.
    def __call__(self):
        asyncio.run(self._run())
        return self.exit_status

    def __repr__(self):
        return(f"<{self.__class__.__name__}('{self.disk_image}', "
               f"mem={self.mem}, port={self.port}, pid={self.pid}, "
               f"state='{self.state}', qmp_established={self.qmp_established}, "
               f"exit_status={self.exit_status})>")

    def _keyboard_interrupt(self, events):
        events['shutdown_request'].set()

    async def _run_vm(self, events):

        # Open log separately for subprocess, which needs to use binary mode
        # (the process will get its own copy of the file handle in any case):
        with open(self.log_file, 'a+b') as log_fd:

            try:
                # Run VM in background session so it doesn't die on ctrl-c,
                # capturing all output to the log:
                proc = await asyncio.create_subprocess_exec(
                       self.cmd,
                       *shlex.split(' '.join(self.cmd_args)), # split opts+args
                       stdin=subprocess.PIPE,
                       stdout=log_fd,
                       stderr=subprocess.STDOUT,
                       start_new_session=True,
                )
                self.pid = proc.pid

                self.log(f'Subprocess Id {self.pid}')

                # Yield control while waiting for process to complete, then
                # save its exit code when it does:
                self.exit_status = await proc.wait()

                # In the event of failure, scrape the log for a memory
                # allocation error (which qemu's exit code doesn't distinguish
                # from other errors), so we can advise the user on what to do.
                # This message appears not to change with the locale setting.
                if self.exit_status == 1:
                    log_fd.seek(0, 0)
                    for line in log_fd:
                        if b'cannot set up guest memory' in line:
                            self.mem_err = True
                            break
                    log_fd.seek(0, 2)  # return to end

            finally:
                # Cancel any tasks that may still be running if the VM didn't
                # start & stop normally, so the script doesn't hang or produce
                # unexpected errors, and set the final machine state:
                self._cancel_tasks('_shut_down', '_wait_until_booted',
                                   '_shutdown_timeout', '_boot_timeout')
                self.state = 'off'

    async def _wait_until_booted(self, events):

        while self.state == 'booting':
            self.log('Attempt ssh connection')
            try:
                await self._check_ssh()
            except ConnectionError:
                await asyncio.sleep(1)  # retry port after 1s
            else:
                self.state = 'running'
                self._cancel_tasks('_boot_timeout')  # unblock event loop exit
                break

        # This coroutine should get cancelled before this ever happens. Don't
        # raise an exception, which would block any coroutines waiting on this.
        if self.state != 'running':
            sys.stderr.write(f'State changed before successful connection '
                             f'to localhost:{self.port}\n')

    async def _check_ssh(self):

        # This usually gives ConnectionRefusedError just on the first try:
        reader, writer = await asyncio.open_connection(
            host=socket.gethostbyname('localhost'), port=self.port
        )
        # This occasionally gets EOF instead of the expected reply (presumably
        # because of some time-out or the server not being ready yet, as when
        # the ssh client briefly produces "read: Connection reset by peer"):
        try:
            reply = await reader.readline()
        finally:
            writer.close()

        self.log(f'Reply {reply} from guest ssh service')

        if not reply.startswith(b'SSH-2.0-'):
            raise ConnectionError(
                f'Bad reply "{reply}" from guest ssh service.'
            )

    async def _progress(self, events):

        shutdown_req_msg = False
        while self.state != 'off':
            char = self.state[0]
            print(char, end='', flush=True)
            if not shutdown_req_msg and events['shutdown_request'].is_set():
                shutdown_req_msg = True
                msg = 'Shutdown requested'
                print('\n' + msg)
                self.log(msg)
            await asyncio.sleep(1)
        # If anything has gone wrong (eg. QMP), add a warning to the status?

    async def _boot_timeout(self, events):

        # Impose a general boot timeout, to avoid hanging indefinitely if the
        # VM itself hangs (but leave long enough for a "normal" fsck before
        # ceding manual control to the user).
        try:
            await asyncio.sleep(300)
        except asyncio.CancelledError:
            return
        if self.state == 'booting':
            sys.stderr.write('\nTimed out.\n')
            self._cancel_tasks()  # May leave QEMU running in the background

    async def _shut_down(self, events):

        # Can't shut down reliably until finished booting (already scheduled):
        await asyncio.wait_for(self._tasks['_wait_until_booted'], timeout=None)

        # If any of the following fails, just log the exception and time out in
        # a separate co-routine, rather than catching the error.

        # Open the QMP socket for communicating with QEMU.
        # Here we'll get a ConnectionRefusedError if the socket can't be opened
        # (eg. because overwritten) or FileNotFoundError if it doesn't exist.
        reader, writer = await asyncio.open_unix_connection(self.qmp_sock)

        try:
            self.log(f'Opened socket {self.qmp_sock}')

            # Negotiate capabilities and enter "command mode", as per the QMP
            # documentation. We might get a JSONDecodeError if the reply is
            # malformed, but I haven't seen that happen.
            reply = await reader.readline()  # returns version & "capabilities"
            writer.write(b'{"execute": "qmp_capabilities"}\r\n')
            reply = json.loads(await reader.readline())
            if 'return' in reply and reply['return'] == {}: # standard response
                self._qmp_established = True
                self.log('Established QMP connection')
            else:
                # Should we re-try here? Don't guess at failure modes not seen.
                raise ConnectionError(
                    f'Failed to establish QMP connection at {self.qmp_sock}'
                )

            # Now we're ready to shut down as soon as the signal arrives:
            await events['shutdown_request'].wait()
            writer.write(b'{"execute": "system_powerdown"}\r\n')
            self.state = 'shutting_down'  # wait for {'event' : 'POWERDOWN'} ?
            self.log('Sent system_powerdown command')
            while self.state != 'off':
                reply = json.loads(await reader.readline())
                if 'event' in reply and reply['event'] == 'SHUTDOWN':
                    # The VM has shut down, but self.state gets set to 'off'
                    # by _run_vm, when the process exits a couple of sec later.
                    # Log this confirmation (& POWERDOWN) in a later version.
                    break

        finally:
            writer.close()

        # The shutdown timer gets cancelled by _run_vm if QEMU now exits as
        # expected, otherwise it keeps running so the user will get control
        # back and an exit status report.

    async def _shutdown_timeout(self, events):

        # Can't shut down until we're asked to *and* the VM has booted:
        await asyncio.wait_for(self._tasks['_wait_until_booted'], timeout=None)

        await events['shutdown_request'].wait()

        await asyncio.sleep(60)  # shut down normally takes a couple of sec.
        sys.stderr.write('\nShut down timed out.\n')
        self._cancel_tasks()

    async def _run(self):

        # Instead of overwriting the log file, delete & (re)create it in
        # append mode, which should guarantee (per POSIX) that QEMU & logging
        # elsewhere in this script won't overwrite each other:
        try:
            os.remove(self.log_file)
        except OSError:
            pass

        # Hold log file open during execution of the main routine:
        with self.log_context() as log_fd:

            loop = asyncio.get_running_loop()
            events = {
                'shutdown_request' : asyncio.Event(),
            }
            loop.add_signal_handler(
                signal.SIGINT,
                functools.partial(self._keyboard_interrupt, events)
            )

            self.state = 'booting'

            self._tasks = {
                name : asyncio.create_task(getattr(self, name)(events)) for
                  name in (
                      '_run_vm',
                      '_wait_until_booted',
                      '_progress',
                      '_boot_timeout',
                      '_shut_down',
                      '_shutdown_timeout',
                  )
            }

            self.log('', time_stamp=False)
            self.log('Starting event loop')

            # Main event loop (saving any exceptions for later):
            retvals = await asyncio.gather(*self._tasks.values(),
                                           return_exceptions=True)

            self.log(f'{self}')

            # Append any internal exceptions to the log:
            errors = []
            for retval in retvals:
                if isinstance(retval, Exception) and not \
                   isinstance(retval, asyncio.CancelledError):
                    errors.append('\n')
                    errors.extend(
                        traceback.format_exception(type(retval), retval,
                                                   retval.__traceback__)
                    )
            if errors:
                self.log(
                    '-'*78 + '\n'
                    'Errors were produced while running the control script:\n' +
                    ''.join(errors),
                    time_stamp=False
                )

    def _cancel_tasks(self, *names):
        if not names:
            names = self._tasks.keys()
        for name in names:
            self._tasks[name].cancel()


if __name__ == '__main__':

    vm = VMControl('qemuiraf.qcow2', mem=mem_GB, port=ssh_port)

    exit_status = vm()

    if exit_status == 0:
        msg = '\nVM process completed successfully'
        print(msg + '\n')
        vm.log(msg, time_stamp=False)

    else:
        end = f'see {vm.log_file}\n'
        if exit_status is None:
            if vm.pid is None:
                msg = f'\nFailed to start VM process: {end}'
            else:
                try:
                    os.kill(vm.pid, 0)  # no-op checks if it's still running
                except ProcessLookupError:
                    # This will probably never happen, because process should
                    # remain as a zombie until the parent gets its exit status:
                    msg = f'\nVM process died uncleanly: {end}'
                else:
                    msg = (f'\nApparently failed to shut down VM process: {end}'
                           f'\nTry logging in with ssh and issuing "sudo '
                           f'shutdown now" manually; otherwise\nkill process '
                           f'{vm.pid} if it\'s unresponsive.\n')
        else:
            if exit_status < 0:
                msg = f'\nVM process killed with signal {-exit_status}: {end}'
            else:
                msg = (f'\nVM process completed with error status '
                       f'{exit_status}: {end}')

        sys.stderr.write(msg + '\n')
        vm.log(msg, time_stamp=False)

        if vm.mem_err:
            msg = (f'It looks like QEMU failed to allocate {mem_GB}GB of '
                   f'contiguous memory to run the VM.\n\n'
                   f'Try restarting large programs such as your Web browser, '
                   f'to reduce memory\nfragmentation (or closing them '
                   f'entirely if that doesn\'t solve it). If the\nproblem '
                   f'persists, try reducing "mem_GB" in the configuration '
                   f'(without going\nbelow 0.25 to 0.5GB, for acceptable '
                   f'performance with a minimal installation).\n')
            sys.stderr.write(msg + '\n')
            vm.log(msg, time_stamp=False)

    sys.exit(1 if exit_status is None else exit_status)
