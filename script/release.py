#!/usr/bin/env python3
#
# A script that detect packages and creates a new release for them.
# It makes a lot of assumptions so make sure all url's given in the
# output makes sense before approving a release.
#

from abc import ABCMeta, abstractmethod
from genericpath import exists
import json
from os import chdir
import os
import re
from shutil import rmtree
import subprocess
import sys
from tempfile import mkdtemp
from typing import Dict, List, NoReturn, Optional
from urllib.request import Request, urlopen


def error(*args) -> NoReturn:
    print("\033[91m" + " ".join(args) + "\033[0m", file=sys.stderr)
    exit(1)


def warn(*args) -> None:
    print("\033[92m" + " ".join(args) + "\033[0m", file=sys.stderr)


def info(*args: str) -> None:
    print("\033[94m" + " ".join(args) + "\033[0m")


def exec(command: str) -> str:
    info(f"Executing '{command}'")
    res = subprocess.run(command.split(" "), capture_output=True)
    if res.returncode != 0:
        error(
            f"Command failed with code {res.returncode}\n", res.stderr.decode("utf-8")
        )
    return res.stdout.decode("utf-8").rstrip()


def request(url: str, headers: Optional[Dict[str, str]] = None) -> str:
    if headers == None:
        headers = {}
    req = Request(url, headers=headers)
    with urlopen(req) as response:
        if response.status != 200:
            error(f"{url} returned {response.status}")
        return response.read().decode("utf-8")


HOME = os.environ["HOME"]
VERSION = sys.argv[1]
pattern = "^[0-9]*\\.[0-9]*\\.[0-9]*$"
if re.match(pattern, VERSION) == None:
    error("No version given")


class Module(metaclass=ABCMeta):
    def name(self) -> str:
        return type(self).__name__

    @abstractmethod
    def should_load(self) -> bool:
        "Should this module load"

    @abstractmethod
    def link(self) -> Optional[str]:
        """Give a link to the project"""

    @abstractmethod
    def validate(self) -> None:
        """Validate that the module has everything it needs to release, to ensure successful release"""

    def pre_release(self) -> None:
        pass

    @abstractmethod
    def release(self) -> None:
        """Make release on given module"""


class Github(Module):
    def should_load(self):
        url = exec("git remote get-url origin")
        return "github.com" in url

    def link(self):
        return exec("gh browse -n")

    def validate(self):
        exec("gh auth status")

    def release(self):
        exec(f"gh release create {VERSION}")


class Aur(Module):
    def should_load(self):
        repo_name = exec("git remote get-url origin")
        git_username = exec("git config --get user.name")
        repo_name = repo_name.split("/")[-1].split(".")[0]

        data = request(
            f"https://aur.archlinux.org/rpc/?v=5&type=search&arg={repo_name}"
        )

        data = json.loads(data)

        results = data["results"]
        filtered_results = []
        for res in results:
            if res["Maintainer"].lower() == git_username.lower():
                filtered_results.append(res)
        # Sort by popularity
        filtered_results.sort(key=lambda r: r["Popularity"])

        if len(filtered_results) == 0:
            return False

        self.package = filtered_results[0].get("Name")
        self.maintainer = filtered_results[0].get("Maintainer")
        return True

    def link(self):
        return f"https://aur.archlinux.org/packages/{self.package}"

    def validate(self):
        # Check if can push to AUR
        tmp = mkdtemp()
        root_path = os.getcwd()
        chdir(tmp)
        exec(f"git clone ssh://aur@aur.archlinux.org/{self.package}.git .")
        exec("git push --dry-run")
        exec("makepkg --help")
        chdir(root_path)
        rmtree(tmp)

    def _sha256sums(self, pkgbuild: str) -> str:
        info("Generating sha256sums")
        pkgbuild_lines = pkgbuild.splitlines()
        sha_lines = []  # all lines containing sha's
        check_ending = False
        for i, line in enumerate(pkgbuild_lines):
            if check_ending:
                sha_lines.append(i)
                if ")" in line:
                    check_ending = False
            if line.startswith("sha256sums="):
                sha_lines.append(i)
                if ")" in line:
                    break
                check_ending = True
        # generate sums
        sum = exec("makepkg -g -f -p PKGBUILD")
        # remove old sums
        sha_lines.reverse()  # remove from big to small
        for i in sha_lines:
            del pkgbuild_lines[i]
        pkgbuild = ""
        # inject new sum in new pkgbuild
        for i, line in enumerate(pkgbuild_lines):
            if i == sha_lines[0] - 1:
                pkgbuild += sum + "\n"
            pkgbuild += line + "\n"
        return pkgbuild.rstrip()

    def release(self):
        tmp = mkdtemp()
        info(f"Created {tmp}")
        root_path = os.getcwd()
        chdir(tmp)
        exec(f"git clone ssh://aur@aur.archlinux.org/{self.package}.git .")

        with open("PKGBUILD", "r") as file:
            pkgbuild: str = file.read()
        pkgbuild = re.sub(
            "^pkgver\\s*=.*", f"pkgver={VERSION}", pkgbuild, 1, re.MULTILINE
        )
        pkgbuild = re.sub("^pkgrel\\s*=.*", f"pkgrel=1", pkgbuild, 1, re.MULTILINE)

        if re.search("^sha256sums\\s*=", pkgbuild, re.MULTILINE):
            pkgbuild = self._sha256sums(pkgbuild)

        with open("PKGBUILD", "w") as file:
            file.write(pkgbuild)

        exec("makepkg --printsrcinfo > .SRCINFO")
        exec("makepkg --check")  # Ensure install works
        # exec("git add PKGBUILD")
        # exec(f"git commit -m 'Release {VERSION}'")
        # exec("git push")

        # chdir(root_path)
        # rmtree(tmp)


class Cargo(Module):
    def should_load(self):
        return exists("Cargo.toml")

    def link(self):
        with open("Cargo.toml", "r") as file:
            name = re.search("^name\\s*=.*", file.read(), re.MULTILINE)
            if name is None:
                error("Could not find crate name")
            name = str(name.group(0)).split("=")[1].replace(" ", "").replace('"', "")

        return f"https://crates.io/crates/{name}"

    def validate(self):
        if not exists(f"{HOME}/.cargo/credentials"):
            error(f"{HOME}/.cargo/credentials does not exist")

        exec("cargo test")
        exec("cargo build --release --locked")
        # exec("cargo publish --dry-run")

    def pre_release(self) -> None:
        info("Updating version in Cargo.toml")
        with open("Cargo.toml", "r") as file:
            cargo_toml: str = file.read()
        cargo_toml = re.sub(
            "^version\\s*=.*", f'version = "{VERSION}"', cargo_toml, 1, re.MULTILINE
        )
        with open("Cargo.toml", "w") as file:
            file.write(cargo_toml)
        exec("git add Cargo.toml")
        exec(f"git commit -m 'Release {VERSION}")
        exec("git push -u")

    def release(self):
        exec(f"cargo publish")


def prepare_branch():
    """Returns the remote"""
    branch = "master"
    if "master" not in exec("git branch"):
        branch = "main"
    remote = exec(f"git config branch.{branch}.remote")

    exec(f"git switch {branch}")
    exec(f"git pull {remote} {branch}")
    return remote


DEFAULT_MODULES = [Cargo(), Github(), Aur()]


def release():
    root = exec("git rev-parse --show-toplevel")
    info(f"Moving to root '{root}'")
    chdir(root)

    # if exec("git status --short") != "":
    #     error("Git branch is dirty")

    prepare_branch()

    modules: List[Module] = []
    for module in DEFAULT_MODULES:
        if module.should_load():
            info(f"Found {module.name()}")
            modules.append(module)

    for module in modules:
        module.validate()

    for module in modules:
        info(f"Will release on {module.name()} ({module.link()})")

    answer = input("Proceed with release? [Y/n] ")
    if answer.lower() != "y":
        error("")

    for module in modules:
        module.pre_release()

    # exec(f"git tag {VERSION}")
    # exec(f"git push {remote} --tags")
    for module in modules:
        module.release()


# release()
aur = Aur()
aur.should_load()
aur.release()
