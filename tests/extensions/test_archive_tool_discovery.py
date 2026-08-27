#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Unit tests for Archive Tool Discovery and CBR/CBZ Archive Handling in Modern Mylar.
"""

import os
import sys
import tempfile
import zipfile
import shutil
import unittest
from unittest.mock import patch, MagicMock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, os.path.join(REPO_ROOT, 'lib'))
sys.path.insert(0, REPO_ROOT)

import mylar
import mylar.config
import mylar.req_test
from mylar.webserve import WebInterface
from lib.comictaggerlib.comicapi.comicarchive import ComicArchive, ZipArchiver
from lib.rarfile import rarfile


class TestArchiveToolDiscovery(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="mylar_archive_test_")
        self.config_path = os.path.join(self.test_dir, "config.ini")
        mylar.PROG_DIR = REPO_ROOT
        mylar.DATA_DIR = self.test_dir
        with open(self.config_path, "w") as f:
            f.write("[General]\n")
            f.write("comic_dir = %s\n" % self.test_dir)
            f.write("cache_dir = %s\n" % self.test_dir)
            f.write("unrar_cmd = \n")
            f.write("ct_settingspath = %s\n" % self.test_dir)
        mylar.CONFIG = mylar.config.Config(self.config_path).read(startup=True)
        mylar.CONFIG.COMIC_DIR = self.test_dir
        mylar.CONFIG.CACHE_DIR = self.test_dir
        mylar.CONFIG.UNRAR_CMD = ""
        mylar.CONFIG.CT_SETTINGSPATH = self.test_dir
        mylar.REQS = {}
        mylar.GLOBAL_MESSAGES = {}

    def tearDown(self):
        try:
            shutil.rmtree(self.test_dir)
        except Exception:
            pass

    def test_01_find_the_unrar_success_with_standard_rarlab_output(self):
        req = mylar.req_test.Req()
        mock_output = MagicMock()
        mock_output.returncode = 0
        mock_output.stderr = ""
        mock_output.stdout = "UNRAR 7.01 freeware      Copyright (c) 1993-2024 Alexander Roshal\nUsage: unrar <command>..."

        with patch("subprocess.run", return_value=mock_output) as mock_run:
            req.find_the_unrar()
            self.assertIn("rar", mylar.REQS)
            self.assertFalse(mylar.REQS["rar"]["rar_failure"])
            self.assertIn("UNRAR 7.01", mylar.REQS["rar"]["rar_message"])
            self.assertEqual(rarfile.UNRAR_TOOL, "unrar")

    def test_02_find_the_unrar_failure_when_executable_missing(self):
        req = mylar.req_test.Req()
        mock_output = MagicMock()
        mock_output.returncode = 127
        mock_output.stderr = "unrar: not recognized as an internal or external command"
        mock_output.stdout = "unrar: not found"

        with patch("subprocess.run", return_value=mock_output):
            req.find_the_unrar()
            self.assertIn("rar", mylar.REQS)
            self.assertTrue(mylar.REQS["rar"]["rar_failure"])
            self.assertEqual(mylar.REQS["rar"]["rar_message"], "Unable to locate unrar")

    def test_03_config_check_omits_unrar_warning_on_success(self):
        mylar.REQS["rar"] = {"rar_failure": False, "rar_message": "UNRAR 7.01"}
        mylar.START_UP = True
        mylar.OS_DETECT = "Linux"
        mylar.CONFIG.CACHE_DIR = self.test_dir
        mylar.CONFIG.COMIC_DIR = self.test_dir

        web = WebInterface()
        web.config_check()

        self.assertNotIn("Could not find a valid unrar executable", str(mylar.GLOBAL_MESSAGES))

    def test_04_config_check_emits_unrar_warning_on_failure(self):
        mylar.REQS["rar"] = {"rar_failure": True, "rar_message": "Unable to locate unrar"}
        mylar.START_UP = True
        mylar.OS_DETECT = "Linux"
        mylar.CONFIG.CACHE_DIR = self.test_dir
        mylar.CONFIG.COMIC_DIR = self.test_dir

        web = WebInterface()
        web.config_check()

        self.assertIn("Could not find a valid unrar executable in the PATH", str(mylar.GLOBAL_MESSAGES))

    def test_05_cbz_zip_archiver_integrity(self):
        cbz_path = os.path.join(self.test_dir, "test_comic.cbz")
        with zipfile.ZipFile(cbz_path, "w") as zf:
            zf.writestr("ComicInfo.xml", "<ComicInfo><Title>Spider-Man</Title></ComicInfo>")
            zf.writestr("001.jpg", b"dummy_jpeg_data")

        ca = ComicArchive(cbz_path)
        self.assertTrue(ca.isZip())
        self.assertFalse(ca.isRar())
        self.assertTrue(ca.seemsToBeAComicArchive())
        page_names = ca.getPageNameList()
        self.assertIn("001.jpg", page_names)
        self.assertTrue(ca.hasCIX())
        cix = ca.readCIX()
        self.assertEqual(cix.title, "Spider-Man")

    def _create_test_rar(self, files):
        import binascii
        buf = bytearray(rarfile.RAR_ID)
        mh = rarfile.S_BLK_HDR.pack(0x90cf, rarfile.RAR_BLOCK_MAIN, 0, 13) + b"\0" * (2 + 4)
        buf.extend(mh)
        for name, data in files.items():
            fname = name.encode("utf-8")
            crc = binascii.crc32(data)
            date = ((2026 - 1980) << 25) + (8 << 21) + (26 << 16)
            fhdr = rarfile.S_FILE_HDR.pack(len(data), len(data), rarfile.RAR_OS_MSDOS, crc, date, 0x14, rarfile.RAR_M0, len(fname), 0x20) + fname
            hlen = rarfile.S_BLK_HDR.size + len(fhdr)
            hdr0 = rarfile.S_BLK_HDR.pack(0, rarfile.RAR_BLOCK_FILE, rarfile.RAR_LONG_BLOCK, hlen) + fhdr
            hcrc = binascii.crc32(hdr0[2:]) & 0xffff
            hdr = rarfile.S_BLK_HDR.pack(hcrc, rarfile.RAR_BLOCK_FILE, rarfile.RAR_LONG_BLOCK, hlen) + fhdr
            buf.extend(hdr)
            buf.extend(data)
        return bytes(buf)

    def test_06_cbr_rar_archiver_integrity(self):
        cbr_data = self._create_test_rar({
            "001.jpg": b"fake_jpeg_data_for_cbr",
            "ComicInfo.xml": b"<ComicInfo><Title>X-Men Classic</Title></ComicInfo>"
        })
        cbr_path = os.path.join(self.test_dir, "test_comic.cbr")
        with open(cbr_path, "wb") as f:
            f.write(cbr_data)

        ca = ComicArchive(cbr_path)
        self.assertTrue(ca.isRar())
        self.assertFalse(ca.isZip())
        self.assertTrue(ca.seemsToBeAComicArchive())
        page_names = ca.getPageNameList()
        self.assertIn("001.jpg", page_names)
        self.assertTrue(ca.hasCIX())
        cix = ca.readCIX()
        self.assertEqual(cix.title, "X-Men Classic")

    def test_07_rarfile_missing_tool_raises_cannot_find_working_tool(self):
        with self.assertRaises(rarfile.RarCannotExec) as ctx:
            rarfile.tool_setup(unrar=False, unar=False, bsdtar=False, force=True)
        self.assertIn("Cannot find working tool", str(ctx.exception))

    def test_08_dockerfile_packaging_and_build_assertion_contract(self):
        dockerfile_path = os.path.join(REPO_ROOT, "Dockerfile")
        self.assertTrue(os.path.isfile(dockerfile_path))

        with open(dockerfile_path, "r", encoding="utf-8") as f:
            df = f.read()

        self.assertIn('ARG UNRAR_VERSION="7.0.9"', df)
        self.assertIn("unrarsrc-${UNRAR_VERSION}.tar.gz", df)
        self.assertIn("install -v -m755 unrar /usr/bin/unrar", df)
        self.assertIn("COPY --from=builder /usr/bin/unrar /usr/bin/unrar", df)
        self.assertIn("libstdc++", df)
        self.assertIn('RUN /usr/bin/unrar 2>&1 | grep -q "Alexander Roshal"', df)


if __name__ == "__main__":
    unittest.main()
