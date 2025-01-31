#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
from typing import Dict, IO, Iterable, Optional, Union

from .MountSource import FileInfo, MountSource
from .utils import overrides


class FolderMountSource(MountSource):
    """
    This class manages one folder as mount source offering methods for listing folders, reading files, and others.
    """

    def __init__(self, path: str) -> None:
        self.root: str = path

    def setFolderDescriptor(self, fd: int) -> None:
        """
        Make this mount source manage the special "." folder by changing to that directory.
        Because we change to that directory it may only be used for one mount source but it also works
        when that mount source is mounted on!
        """
        os.fchdir(fd)
        self.root = '.'

    def _realpath(self, path: str) -> str:
        """Path given relative to folder root. Leading '/' is acceptable"""
        return os.path.join(self.root, path.lstrip(os.path.sep))

    @staticmethod
    def _statsToFileInfo(stats: os.stat_result, path: str, linkname: str):
        return FileInfo(
            # fmt: off
            size     = stats.st_size,
            mtime    = stats.st_mtime,
            mode     = stats.st_mode,
            linkname = linkname,
            uid      = stats.st_uid,
            gid      = stats.st_gid,
            userdata = [path],
            # fmt: on
        )

    @staticmethod
    def _dirEntryToFileInfo(dirEntry: os.DirEntry, path: str, realpath: str):
        try:
            linkname = os.readlink(realpath) if dirEntry.is_symlink() else ""
        except OSError:
            linkname = ""

        return FolderMountSource._statsToFileInfo(dirEntry.stat(follow_symlinks=False), linkname, path)

    @overrides(MountSource)
    def isImmutable(self) -> bool:
        return False

    @overrides(MountSource)
    def exists(self, path: str) -> bool:
        return os.path.lexists(self._realpath(path))

    @overrides(MountSource)
    def getFileInfo(self, path: str, fileVersion: int = 0) -> Optional[FileInfo]:
        """All returned file infos contain a file path string at the back of FileInfo.userdata."""

        # This is a bit of problematic design, however, the fileVersions count from 1 for the user.
        # And as -1 means the last version, 0 should also mean the first version ...
        # Basically, I did accidentally mix user-visible versions 1+ versions with API 0+ versions,
        # leading to this problematic clash of 0 and 1.
        if fileVersion not in [0, 1] or not self.exists(path):
            return None

        realpath = self._realpath(path)
        linkname = os.readlink(realpath) if os.path.islink(realpath) else ""
        return self._statsToFileInfo(os.lstat(realpath), path, linkname)

    @overrides(MountSource)
    def listDir(self, path: str) -> Optional[Union[Iterable[str], Dict[str, FileInfo]]]:
        realpath = self._realpath(path)
        if not os.path.isdir(realpath):
            return None

        return {
            os.fsdecode(dirEntry.name): FolderMountSource._dirEntryToFileInfo(dirEntry, path, realpath)
            for dirEntry in os.scandir(realpath)
        }

    @overrides(MountSource)
    def fileVersions(self, path: str) -> int:
        return 1 if self.exists(path) else 0

    @overrides(MountSource)
    def open(self, fileInfo: FileInfo) -> IO[bytes]:
        path = fileInfo.userdata[-1]
        assert isinstance(path, str)
        realpath = self._realpath(path)

        try:
            return open(realpath, 'rb')
        except Exception as e:
            raise ValueError(f"Specified path '{realpath}' is not a file that can be read!") from e

    @overrides(MountSource)
    def read(self, fileInfo: FileInfo, size: int, offset: int) -> bytes:
        with self.open(fileInfo) as file:
            file.seek(offset, os.SEEK_SET)
            return file.read(size)

    def getFilePath(self, fileInfo: FileInfo) -> str:
        path = fileInfo.userdata[-1]
        assert isinstance(path, str)
        return self._realpath(path)
