"""Opening a saved copy, choosing where recovered files go, searching, and thumbnails."""
import base64
import os
import struct
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from drive_rescue import images, plan as plan_mod, recover, server  # noqa: E402
from test_server import AppCase  # noqa: E402


def make_image(folder, name="WDC-1234.img", size=2 * 1024 * 1024, mapfile=True):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    with open(path, "wb") as fh:
        fh.truncate(size)
    if mapfile:
        with open(os.path.splitext(path)[0] + ".map", "w") as fh:
            fh.write(f"0x0 + 1\n0x0 {size // 2} +\n{hex(size // 2)} {size // 2} -\n")
    return path


class FindTests(unittest.TestCase):
    def test_finds_images_newest_first_and_skips_system_folders(self):
        tmp = tempfile.mkdtemp()
        a = make_image(os.path.join(tmp, "DriveRescue"), "a.img")
        b = make_image(os.path.join(tmp, "Old", "deep"), "b.img", mapfile=False)
        make_image(os.path.join(tmp, "Windows"), "c.img")          # a system folder
        make_image(os.path.join(tmp, "x", "y", "z", "w"), "d.img")  # too deep
        open(os.path.join(tmp, "small.img"), "wb").write(b"x")     # too small to be a drive copy
        os.utime(a, (1000, 1000))
        os.utime(b, (2000, 2000))
        found = images.scan(tmp)
        self.assertEqual([i["name"] for i in found], ["b.img", "a.img"])
        self.assertEqual(found[1]["percent"], 50.0)
        self.assertIsNone(found[0]["mapfile"])

    def test_the_drive_model_comes_from_the_job_file(self):
        tmp = tempfile.mkdtemp()
        p = make_image(tmp, "q.img")
        open(os.path.splitext(p)[0] + ".job.json", "w").write('{"source": {"model": "WDC Blue 1TB", "serial": "S1"}}')
        self.assertEqual(images.scan(tmp)[0]["model"], "WDC Blue 1TB")


class AppTests(AppCase):
    def setUp(self):
        super().setUp()
        self.image = make_image(os.path.join(self.sys.dest_mount, "DriveRescue"))

    def test_find_and_open_a_saved_copy(self):
        r = self.call("GET", "image/find", query={"where": [self.dst]})
        self.assertEqual(len(r["images"]), 1)
        self.assertEqual(r["images"][0]["percent"], 50.0)
        self.call("POST", "image/open", {"id": r["images"][0]["id"]})
        self.assertEqual(self.app.plan["mode"], "image")
        self.assertEqual(self.app.plan["image"], self.image)
        self.assertEqual(self.call("GET", "status")["state"], "none")

    def test_only_listed_copies_can_be_opened(self):
        self.call("GET", "image/find", query={"where": [self.dst]})
        with self.assertRaises(server.ApiError):
            self.call("POST", "image/open", {"id": "../../etc/passwd"})

    def test_the_rescue_usb_and_unknown_drives_are_refused(self):
        with self.assertRaises(server.ApiError):
            self.call("GET", "image/find", query={"where": ["/dev/fake-sdc1"]})
        with self.assertRaises(server.ApiError):
            self.call("GET", "image/find", query={"where": ["/dev/nope"]})

    def test_recovery_from_a_saved_copy_needs_a_chosen_destination(self):
        r = self.call("GET", "image/find", query={"where": [self.dst]})
        self.call("POST", "image/open", {"id": r["images"][0]["id"]})
        with self.assertRaises(server.ApiError):
            self.app._recover_dest(None)
        with self.assertRaises(server.ApiError):
            self.app._recover_dest({"kind": "same"})

    def test_a_partition_destination_gets_its_own_folder(self):
        out = self.app._recover_dest({"kind": "partition", "path": self.dst, "folder": "My Photos!"})
        self.assertEqual(out, os.path.join(self.sys.dest_mount, "My Photos"))

    def test_the_failing_drive_and_the_rescue_stick_are_never_destinations(self):
        self.app.plan = {"mode": "rescue", "source": {"path": self.src}, "target_dir": "/x"}
        with self.assertRaises(server.ApiError):
            self.app._recover_dest({"kind": "partition", "path": "/dev/fake-sdc1"})

    def test_network_destination_needs_a_connection(self):
        with self.assertRaises(server.ApiError):
            self.app._recover_dest({"kind": "network"})
        self.app.network = {"mountpoint": "/mnt/n", "label": "nas/Backup"}
        self.assertEqual(self.app._recover_dest({"kind": "network", "folder": "Saved"}), "/mnt/n/Saved")

    def test_a_report_is_possible_without_a_copy_in_this_session(self):
        r = self.call("GET", "image/find", query={"where": [self.dst]})
        self.call("POST", "image/open", {"id": r["images"][0]["id"]})
        rep = self.call("GET", "report")
        self.assertIn(self.image, rep["text"])


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        for rel in ("Photos/2020/beach.jpg", "Photos/cat.PNG", "Docs/Tax Return.pdf", "Docs/notes.txt", "song.mp3"):
            path = os.path.join(self.tmp, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "wb").write(b"data")
        os.symlink("/etc", os.path.join(self.tmp, "link"))
        rec = recover.Recovery(None, "x.img", self.tmp)
        rec.volumes = [{"id": "0", "name": "V", "mountpoint": self.tmp}]
        self.rec = rec

    def names(self, r):
        return [e["name"] for e in r["entries"]]

    def test_search_by_name_ignores_case_and_looks_in_every_folder(self):
        self.assertEqual(self.names(self.rec.search("0", "BEACH")), ["beach.jpg"])
        self.assertEqual(self.rec.search("0", "tax")["entries"][0]["folder"], "Docs")

    def test_filter_by_kind(self):
        self.assertEqual(self.names(self.rec.search("0", "", "photo")), ["beach.jpg", "cat.PNG"])
        self.assertEqual(self.names(self.rec.search("0", "", "audio")), ["song.mp3"])
        self.assertEqual(self.names(self.rec.search("0", "o", "document")), ["notes.txt"])

    def test_links_are_not_followed_and_empty_searches_are_refused(self):
        self.assertNotIn("passwd", " ".join(self.names(self.rec.search("0", "pass"))))
        with self.assertRaises(recover.RecoverError):
            self.rec.search("0", "", "")
        with self.assertRaises(recover.RecoverError):
            self.rec.search("0", "a", "nonsense")

    def test_a_slow_search_stops_and_says_so(self):
        ticks = iter(range(0, 1000, 6))
        r = self.rec.search("0", "a", "", clock=lambda: next(ticks))
        self.assertTrue(r["truncated"])

    def test_file_types(self):
        self.assertEqual(recover.file_type("A.JPG"), "photo")
        self.assertEqual(recover.file_type("x.docx"), "document")
        self.assertEqual(recover.file_type("README"), "")


class ThumbTests(unittest.TestCase):
    def make_jpeg(self, thumb):
        tiff = b"MM\x00\x2a" + struct.pack(">I", 8)
        ifd0 = struct.pack(">H", 0) + struct.pack(">I", 14)
        ifd1 = (struct.pack(">H", 2) + struct.pack(">HHII", 0x0201, 4, 1, 14 + 2 + 24 + 4)
                + struct.pack(">HHII", 0x0202, 4, 1, len(thumb)) + struct.pack(">I", 0))
        app1 = b"Exif\0\0" + tiff + ifd0 + ifd1 + thumb
        return b"\xff\xd8\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1 + b"\xff\xdb\0\x04\0\0\xff\xda" + b"x" * 5000

    def test_reads_the_photos_own_small_picture(self):
        thumb = b"\xff\xd8\xff\xdb" + b"t" * 30 + b"\xff\xd9"
        self.assertEqual(recover.exif_thumbnail(self.make_jpeg(thumb)), thumb)

    def test_broken_or_missing_data_gives_nothing_instead_of_an_error(self):
        self.assertIsNone(recover.exif_thumbnail(b"not a jpeg"))
        self.assertIsNone(recover.exif_thumbnail(b"\xff\xd8\xff\xe1\x00\x10Exif\0\0MM"))
        self.assertIsNone(recover.exif_thumbnail(b"\xff\xd8\xff\xdb\0\x04\0\0\xff\xda"))

    def test_thumbnail_uses_the_embedded_one_and_refuses_fake_pictures(self):
        tmp = tempfile.mkdtemp()
        thumb = b"\xff\xd8\xff\xdb" + b"t" * 30 + b"\xff\xd9"
        open(os.path.join(tmp, "a.jpg"), "wb").write(self.make_jpeg(thumb))
        open(os.path.join(tmp, "fake.jpg"), "wb").write(b"this is not a picture")
        open(os.path.join(tmp, "big.png"), "wb").write(b"\x89PNG\r\n\x1a\n" + b"0" * (2 * 1024 * 1024))
        rec = recover.Recovery(None, "x.img", tmp)
        rec.volumes = [{"id": "0", "name": "V", "mountpoint": tmp}]
        self.assertEqual(base64.b64decode(rec.thumbnail("0", "a.jpg")["data"]), thumb)
        self.assertEqual(rec.thumbnail("0", "fake.jpg"), {})
        self.assertEqual(rec.thumbnail("0", "big.png"), {})      # too big to be worth sending
        self.assertEqual(rec.thumbnail("0", "missing.jpg"), {})


if __name__ == "__main__":
    unittest.main()
