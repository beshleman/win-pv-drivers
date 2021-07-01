#!/usr/bin/env python

import argparse
import logging
import os
import pprint
import shutil
import sys
import tempfile
from contextlib import contextmanager
from subprocess import PIPE, SubprocessError, call, run, CompletedProcess
from typing import NoReturn, Optional, List
from zipfile import ZipFile
import branding

import config

CERT_MGR = None
MAKE_CERT = None
DRIVES = [letter + ":" for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"]

PROG = os.path.basename(sys.argv[0])


def do_run(*args, **kwargs) -> CompletedProcess:
    """
    Execute command using subprocess.run().

    Forces check=True in subprocess.run().
    Logs the full command passed to subprocess.run().

    Returns the Completed Process.
    """
    if "check" not in kwargs:
        kwargs["check"] = True
    log_run(*args, **kwargs)
    return run(*args, **kwargs)


def log_run(*args, **kwargs):
    args_string = ", ".join(pprint.pformat(arg) for arg in args)
    kwargs_copy = kwargs.copy()
    # The environment is a VERY large dictionary, so lets not print it
    kwargs_copy.pop("env", None)
    kwargs_string = pprint.pformat(kwargs_copy)
    run_string = "run(args={}, kwargs={}".format(args_string, kwargs_string)
    logging.debug("Invoking: " + run_string)


def perror(message) -> None:
    """Print an error message to stderr."""
    print("ERROR: " + message, file=sys.stderr)
    logging.error(message)


def die(message) -> NoReturn:
    """Print an error message to stderr and exit with exit code 1."""
    perror(message)
    sys.exit(1)


def setup_env() -> None:
    """Setup environment variables used by the build system."""
    global CERT_MGR
    global MAKE_CERT

    if not os.environ.get("BUILD_ENV", None):
        logging.debug("Enviroment variable BUILD_ENV not found")
        # BUILD_ENV points into the EWDK. Because it is usually a mounted ISO,
        # we search for the EWDK directory structure at the root of each
        # possible drive.
        for drive in DRIVES:
            path = os.path.normpath(os.path.join(drive, "BuildEnv"))
            logging.debug(f"Searching for {path}")

            if os.path.exists(os.path.join(path, "SetupBuildEnv.cmd")):
                os.environ["BUILD_ENV"] = os.path.normpath(path)
                logging.debug(
                    f'Environment variable BUILD_ENV set to {os.environ["BUILD_ENV"]}'
                )
                break

    # Set $ENV:VS = <path to VS 2012>, if it exists
    systemdir = os.path.join("C:", "/", "Program Files (x86)")
    if "VS" not in os.environ:
        logging.debug("Environment variable VS not found")
        directory = os.path.join(systemdir, "Microsoft Visual Studio 11.0")
        path = os.path.join(directory, "VC", "vcvarsall.bat")
        if os.path.exists(path):
            os.environ["VS"] = os.path.normpath(directory)
            logging.debug(f'Environment variable VS set to {os.environ["VS"]}')

    # Set $ENV:WIX = <path to WiX 3.6>, if it exists
    if "WIX" not in os.environ:
        logging.debug("Environment variable WIX not found")
        path = os.path.join(systemdir, "WiX Toolset v3.6")

        if os.path.exists(path):
            os.environ["WIX"] = os.path.normpath(path)
            logging.info(f"Environment variable WIX set to {os.environ['WIX']}")

    # Set $ENV:KIT = <path to Windows 8 Kit>, if it exists
    if "KIT" not in os.environ:
        logging.debug("Environment variable KIT not found")
        kit = os.path.join(systemdir, "Windows Kits", "8.0")
        makecert = os.path.join(kit, "bin", "x64", "makecert.exe")
        certmgr = os.path.join(kit, "bin", "x64", "certmgr.exe")

        if os.path.exists(makecert) and os.path.exists(certmgr):
            os.environ["KIT"] = kit

    if "KIT" in os.environ:
        logging.info(f"Environment variable KIT set to {os.environ['KIT']}")
        kit = os.path.join(systemdir, "Windows Kits", "8.0")
        MAKE_CERT = os.path.normpath(os.path.join(kit, "bin", "x64", "makecert.exe"))
        CERT_MGR = os.path.normpath(os.path.join(kit, "bin", "x64", "certmgr.exe"))


def check_env() -> None:
    """Die if any required environment variable is not defined."""
    vars = {
        "BUILD_ENV",
        "WIX",
        "KIT",
        "VS",
    }

    missing = vars - set(os.environ.keys())
    if missing:
        die("Please set the following environment variables: %s" % ", ".join(missing))

    logging.info("All environment variables found.")
    for var in sorted(list(vars)):
        logging.info("%s = %s" % (var, os.environ[var]))


def fetch() -> None:
    """Fetch all repos."""
    for url, branch in config.REPOS:
        do_run(["git", "clone", url], check=False)


def url_to_simple_name(url) -> str:
    """
    Reduce URL to name of git repo.

    For example, "https://www.github.com/xcp-ng/win-xenvbd.git" becomes "win-xenvbd"

    Returns the name of the git repo with no .git extension.
    """
    return os.path.basename(url).split(".git")[0]


PROJECTS = [url_to_simple_name(url) for url, _ in config.REPOS]


@contextmanager
def change_dir(directory: str, *args, **kwds):
    """
    Temporarily changes the current directory.

    Changes to a directory when entering the context, returns to
    the previous directory when exiting the context.

    Usage:

    >>> with change_dir("path/to/dir/"):
    >>>     do_stuff_in_new_directory()
    >>> do_stuff_in_previous_directory()

    Returns None.
    """

    def __chdir(path):
        os.chdir(path)
        logging.debug("Changed working directory to %s" % os.path.abspath(path))

    prevdir = os.path.abspath(os.curdir)
    __chdir(directory)
    try:
        yield
    finally:
        __chdir(prevdir)


def check_projects(projects: List[str]) -> None:
    """Check that a list of projects is valid."""
    rem = set(projects) - set(PROJECTS)
    if rem:
        die(
            "project(s) %s not valid.  Options are: %s" % (", ".join(rem), PROJECTS)
        )


def ewdk_cmd(cmd: str, *args, **kwargs) -> CompletedProcess:
    """
    Execute a command inside a EWDK build environment.

    Returns a CompletedProcess.
    """
    build_env = os.path.normpath(
        os.path.join(os.environ["BUILD_ENV"], "SetupBuildEnv.cmd")
    )
    kwargs["shell"] = True
    return do_run(
        ["cmd.exe", "/C", "call %s && %s" % (build_env, cmd)],
        env=os.environ.copy(),
        *args,
        **kwargs,
    )


def do_cmd(cmd: str, *args, **kwargs) -> CompletedProcess:
    """
    Execute a simple command.

    Returns a CompletedProcess.
    """
    return do_run(cmd.split(), env=os.environ.copy(), *args, **kwargs)


def build(projects: List[str], checked: bool) -> None:
    """Build all source repos if projects is empty, otherwise build only those repos found in projects."""
    check_projects(projects)

    # Place installer at the end of the list
    projects.sort(key=lambda proj: 1 if "installer" in proj else 0)

    passed = []
    failed = []
    for i, dirname in enumerate(PROJECTS):
        if "installer" in dirname:
            build_installer()
            continue

        assert os.path.exists(
            dirname
        ), "Source directory %s does not exist, has '%s fetch' been executed?" % (
            dirname,
            PROG,
        )

        if projects and dirname not in projects:
            continue

        cmd = ewdk_cmd if not "win-xenguestagent" == dirname else do_cmd

        with change_dir(dirname):
            py_script = os.path.join(os.curdir, "build.py")
            ps_script = os.path.join(os.curdir, "build.ps1")
            buildarg = "checked" if checked else "free"
            # TODO: make toggleable the "checked" option by a --debug flag
            try:
                if os.path.exists(py_script):
                    p = cmd("python %s %s" % (py_script, buildarg))
                elif os.path.exists(ps_script):
                    p = cmd("powershell -file %s %s" % (ps_script, buildarg))
                else:
                    failed.append(dirname)
                    continue
            except SubprocessError:
                failed.append(dirname)
                continue
            passed.append(dirname)


        msg = ""
        if passed:
            msg += "Passed: " + ", ".join(passed)
        if failed:
            msg += "\nFailed: " + ", ".join(failed)
        print(msg)


def create_installer_dep_directory() -> str:
    """Create the directory of dependencies that the installer build requires."""
    depdir = tempfile.mkdtemp(prefix="xen_installer_")
    print("Installer dependency directory: %s" % depdir)

    for proj in PROJECTS:
        if proj == "win-installer":
            continue
        winless = proj.split("win-")[1]
        olddir = os.path.join(proj, winless)
        newdir = os.path.join(depdir, winless)
        shutil.copytree(olddir, newdir)

    shutil.copytree(
        os.path.join("win-installer", "src", "vmcleaner"),
        os.path.join(depdir, "vmcleaner"),
    )

    logging.debug("Install Dependency Directory Contents:")
    for directory, _, files in os.walk(depdir):
        for file in files:
            fpath = os.path.join(directory, file)
            logging.debug("\t%s" % fpath)
    return depdir


def authenticode_thumbprint(file: str) -> str:
    """
    Return the x509 certificate thumbprint from an Authenticode file.

    Arguments:
    ---------
        file: Path to authenticode file (for example, a .exe or .msi file).

    """
    command = "powershell.exe (Get-AuthenticodeSignature -FilePath {}).SignerCertificate.Thumbprint".format(
        file
    )
    return do_cmd(command, stdout=PIPE).stdout.strip().decode()


def certificate_thumbprint(cert: str) -> str:
    """
    Return the thumbprint from an x509 certificate.

    Arguments:
    ---------
        cert: Path to the certificate file.

    """
    command = (
        "powershell.exe (New-Object System.Security.Cryptography.X509Certificates.X509Certificate2 '%s')"
        ".Thumbprint"
    ) % os.path.abspath(cert)
    return do_cmd(command, stdout=PIPE).stdout.strip().decode()


def validate_authenticode_cert(cert: str, authenticode_file: str) -> None:
    """
    Return True if the authenticode file is signed by a specific cert.  Otherwise, return False.

    Arguments:
    ---------
        cert: The certificate for which you want to determine whether or not it was used to
              sign the authenticode file.
        authenticode_file: the Authenticode file to be tested.

    """
    auth_thumb = authenticode_thumbprint(authenticode_file)
    cert_thumb = certificate_thumbprint(cert)
    print(
        "Comparing thumbprints %s (%s) and %s (%s)"
        % (cert, cert_thumb, authenticode_file, auth_thumb)
    )
    if auth_thumb != cert_thumb:
        die(
            "{}'s thumbprint ({}) does not match {}'s thumbprint ({})".format(
                cert, cert_thumb, authenticode_file, auth_thumb
            )
        )


def build_installer() -> None:
    """Build the installer and print out its location."""
    if not os.path.exists("win-installer"):
        err = "Source directory 'win-installer' does not exist, has '%s fetch' been executed?"
        die(err % PROG)

    failed = False
    try:
        certname = sign_and_build_installer()
        directory = create_output_directory()
        zip_path = create_zipfile("win-pv-drivers.zip", directory)
    except SubprocessError:
        failed = True

    if not failed:
        print("SUCCESS: the installer may be found here: %s" % directory)
        print("Certificate Used:", certname)
        print("All output files bundled into file:", zip_path)
    else:
        perror("Building the installer failed")


def sign_and_build_installer():
    """Prepare the dependency directory and call the build script for win-installer."""
    depdir = create_installer_dep_directory()
    certname = "%s(test)" % branding.branding["manufacturer"]
    with change_dir("win-installer"):
        do_cmd("python build.py --local %s --sign %s" % (depdir, certname))
    return certname


def create_output_directory() -> str:
    """
    Return the output directory.

    If the output directory doesn't exist, it is created.
    """
    directory = os.path.abspath("output")
    if not os.path.exists(directory):
        os.mkdir(directory)
    copy_installer_files(directory)
    return directory


def copy_installer_files(directory: str) -> None:
    """Copy win-installer files into directory."""
    installer = os.path.join("win-installer", "installer")
    for fname in ["managementagentx64.msi", "managementagentx86.msi", "Setup.exe"]:
        fpath = os.path.join(installer, fname)
        shutil.copy(fpath, directory)


def create_zipfile(name: str, directory: str) -> str:
    """
    Create a zip file.

    The output zip file is also placed inside the 'directory'.

    Return the path to the zip file.

    Arguments:
    ---------
        name: The name of the zip file. It should include the .zip extension.
        directory: The directory containing the contents to include in the
                   zip file. The directory must exist.

    """
    zip_path = os.path.join(directory, name)
    with ZipFile(zip_path, "w") as zip:
        # Use the name here to avoid adding the full abs path to the zip file
        for f in os.listdir(directory):
            # If there is a stale zip file in the directory, don't add
            # it to the new zip file.
            if f == os.path.basename(zip_path):
                continue
            zip.write(os.path.join(directory, f), arcname=f)
    return zip_path


def prepare_cert(filename: str, certname: str) -> None:
    """
    Create a test signing cert.

    This cert will be used by win-installer to sign the installer and drivers,
    and the cert may used for testing on a test machine.

    Arguments:
    ---------
        filename: the output file name
        certname: the name of the cert (i.e., the CN)

    """
    global MAKE_CERT
    do_run(
        [
            MAKE_CERT,
            "-r",
            "-pe",
            "-ss",
            "my",  # the Personal store, required by win-installer
            "-n",
            "CN=%s" % certname,
            "-eku",
            "1.3.6.1.5.5.7.3.3",
            filename,
        ],
        env=os.environ.copy(),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="The Windows PV drivers builder.")
    parser.set_defaults(action=None)

    parser.add_argument("--loglevel", choices=["DEBUG", "INFO", "ERROR"])

    subparsers = parser.add_subparsers()
    fetch_parser = subparsers.add_parser("fetch", help="Fetch all source repos.")
    fetch_parser.set_defaults(action="fetch")

    build_parser = subparsers.add_parser("build", help="Build all source repos.")
    build_parser.set_defaults(action="build")

    build_parser.add_argument("projects", nargs="*", choices=PROJECTS + [[]], help="The projects to build.")

    build_parser.add_argument(
        "--debug", "-d", action="store_true", help="Build projects with debug config."
    )

    args = parser.parse_args()

    logging.basicConfig(
        level={
            "INFO": logging.INFO,
            "DEBUG": logging.DEBUG,
            "ERROR": logging.ERROR,
        }.get(args.loglevel, logging.INFO)
    )

    if args.action == "fetch":
        fetch()
    elif args.action == "build":
        setup_env()
        check_env()
        build(args.projects, checked=args.debug)
    else:
        parser.print_help()
        sys.exit(1)
