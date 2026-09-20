"""HealthIAM as a Windows service.

Registered and controlled through pywin32's own command line:

    python healthiam_service.py install      # then set the logon account, see Install-HealthIAM.ps1
    python healthiam_service.py start|stop|remove

pywin32 rather than a wrapper like NSSM or WinSW because it installs from PyPI with
everything else. A hospital network that will not let a server reach an unfamiliar site
to download an .exe will still have a PyPI mirror, and one dependency list beats one
dependency list plus a binary somebody has to vet.

Service failures before logging is up land in the Windows event log (Event Viewer >
Windows Logs > Application, source "HealthIAM"). Anything after that goes to LOG_FILE.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

import servicemanager
import win32event
import win32service
import win32serviceutil

BASE_DIR = Path(__file__).resolve().parent.parent.parent


class HealthIAMService(win32serviceutil.ServiceFramework):
    _svc_name_ = "HealthIAM"
    _svc_display_name_ = "HealthIAM"
    # Host the service in the virtual environment's own python.exe rather than pywin32's
    # pythonservice.exe. That helper resolves pywintypes3xx.dll through the process DLL
    # search path and fails as a bare "Error 1053" when it cannot -- which is the usual
    # outcome unless pywin32_postinstall has copied those DLLs into System32. Running
    # python.exe uses pywin32's normal import bootstrap and needs no such step.
    #
    # -X utf8 has to be an interpreter flag: setting PYTHONUTF8 from inside the process is
    # already too late. Without it stdio and the log file default to the ANSI code page,
    # and the first directory entry with a character outside it -- an accented name in a
    # DN, a group description -- raises UnicodeEncodeError from the logging call.
    _exe_name_ = sys.executable
    _exe_args_ = f'-u -X utf8 "{Path(__file__).resolve()}"'
    _svc_description_ = (
        "HealthIAM application catalog and position-based access defaults. Serves the web "
        "application on 127.0.0.1:8000 for the IIS reverse proxy in front of it."
    )

    def __init__(self, args):
        super().__init__(args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        self.server = None

    def SvcStop(self):
        # An in-flight directory sync can hold a thread for minutes; asking for the time
        # keeps the SCM from reporting the stop as hung.
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=60000)
        if self.server is not None:
            # Wakes waitress out of its accept loop; in-flight requests are given the
            # chance to finish rather than being cut off mid-response.
            self.server.close()
        win32event.SetEvent(self.stop_event)

    def SvcDoRun(self):
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        try:
            # The SCM kills a service that has not reported SERVICE_RUNNING within 30
            # seconds. Importing Django and connecting to a database that is still
            # starting after a reboot can take longer than that, so ask for more time
            # before doing any of it. (Migrations deliberately do not run here at all --
            # they are the installer's job, where nothing is on a deadline.)
            self.ReportServiceStatus(win32service.SERVICE_START_PENDING, waitHint=30000)

            # A service starts in C:\Windows\System32. Django itself does not care --
            # every path in settings is derived from BASE_DIR -- but management commands,
            # relative paths in .env and any traceback read far better from the install
            # root, and sys.path must contain it for `config` and `apps` to import.
            os.chdir(BASE_DIR)
            if str(BASE_DIR) not in sys.path:
                sys.path.insert(0, str(BASE_DIR))

            from deploy.windows import serve

            self.server = serve.build_server()
            self.server.run()
        except Exception:
            # Without this the service dies as a bare "error 1053: the service did not
            # respond in a timely fashion" and the reason is nowhere. Most often it is a
            # missing tzdata, an unreachable database or a malformed .env.
            servicemanager.LogErrorMsg(
                f"HealthIAM failed to start:\n{traceback.format_exc()}\n"
                f"Run serve.py in the foreground from {BASE_DIR} to see this interactively."
            )
            raise


if __name__ == "__main__":
    # `python healthiam_service.py` with no arguments is how the service control manager
    # launches it; with arguments it is the operator installing or removing the service.
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(HealthIAMService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(HealthIAMService)
