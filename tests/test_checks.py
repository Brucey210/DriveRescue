"""The sample check of a finished copy, the quick read test, and the checklist."""
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from drive_rescue import assess, verify  # noqa: E402


class SampleCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.src, self.img = os.path.join(self.tmp, "src"), os.path.join(self.tmp, "img")
        self.data = os.urandom(4 * 1024 * 1024)
        open(self.src, "wb").write(self.data)
        open(self.img, "wb").write(self.data)
        self.segments = [(0, len(self.data), "+")]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_good_copy_matches(self):
        r = verify.sample_check(self.src, self.img, self.segments, count=8, chunk=65536)
        self.assertTrue(r["ok"])
        self.assertEqual(r["checked"], 8)

    def test_a_flipped_byte_in_the_copy_is_found(self):
        bad = bytearray(self.data)
        for i in range(0, len(bad), 4096):  # damage every page so any sample hits it
            bad[i] ^= 0xFF
        open(self.img, "wb").write(bytes(bad))
        r = verify.sample_check(self.src, self.img, self.segments, count=8, chunk=65536)
        self.assertFalse(r["ok"])
        self.assertTrue(r["different"])

    def test_only_copied_areas_are_sampled(self):
        segs = [(0, 1 << 20, "+"), (1 << 20, 1 << 20, "-"), (2 << 20, 1 << 20, "+")]
        places = verify.pick_samples(segs, 20, 65536)
        self.assertTrue(places)
        for pos, length in places:
            self.assertFalse(1 << 20 <= pos < 2 << 20, places)
            self.assertFalse(pos < 1 << 20 < pos + length)

    def test_a_source_that_will_not_read_again_is_not_called_a_difference(self):
        r = verify.sample_check(os.path.join(self.tmp, "gone"), self.img, self.segments, count=4, chunk=65536)
        self.assertEqual((r["checked"], r["unreadable"], r["ok"]), (0, 4, True))

    def test_nothing_copied_means_nothing_to_check(self):
        self.assertEqual(verify.pick_samples([(0, 100, "-")], 10, 4096), [])

    def test_cancelling_stops_early(self):
        r = verify.sample_check(self.src, self.img, self.segments, count=8, chunk=65536, cancelled=lambda: True)
        self.assertEqual(r["checked"], 0)


class ReadTestTests(unittest.TestCase):
    def test_spots_spread_over_the_whole_drive(self):
        places = assess.spots(1_000_000_000, 40)
        self.assertEqual(places[0], 0)
        self.assertEqual(len(places), 40)
        self.assertLessEqual(places[-1] + assess.SPOT_BYTES, 1_000_000_000)
        self.assertEqual(places, sorted(places))

    def test_a_healthy_drive_rates_good(self):
        r = assess.read_test("x", 10 ** 9, reader=lambda off: 4096)
        self.assertEqual((r["rating"], r["errors"], r["hung"]), ("good", 0, 0))

    def test_errors_and_stuck_reads_lower_the_rating(self):
        calls = {"n": 0}

        def reader(off):
            calls["n"] += 1
            if calls["n"] % 3 == 0:
                raise OSError(5, "io")
            return 4096
        r = assess.read_test("x", 10 ** 9, reader=reader)
        self.assertIn(r["rating"], ("poor", "bad"))
        self.assertGreater(r["errors"], 10)

    def test_a_read_that_never_returns_cannot_hang_the_test(self):
        import threading
        block = threading.Event()

        def reader(off):
            if off > 0:
                block.wait(30)
            return 4096
        r = assess.read_test("x", 10 ** 9, reader=reader, time_limit=0.5)
        block.set()
        self.assertGreater(r["hung"], 30)
        self.assertEqual(r["rating"], "bad")

    def test_ratings(self):
        self.assertEqual(assess.rate(0, 0, 0, 40), "good")
        self.assertEqual(assess.rate(0, 2, 0, 40), "fair")
        self.assertEqual(assess.rate(4, 0, 0, 40), "poor")
        self.assertEqual(assess.rate(30, 0, 0, 40), "bad")


if __name__ == "__main__":
    unittest.main()


class TidyTests(unittest.TestCase):
    def setUp(self):
        from drive_rescue import tidy
        self.tidy = tidy
        self.tmp = tempfile.mkdtemp()
        d1, d2 = os.path.join(self.tmp, "recup_dir.1"), os.path.join(self.tmp, "recup_dir.2")
        os.makedirs(d1)
        os.makedirs(d2)
        self.photo = os.urandom(5000)
        open(os.path.join(d1, "f1.jpg"), "wb").write(self.photo)
        open(os.path.join(d2, "f1.jpg"), "wb").write(os.urandom(6000))     # same name, different file
        open(os.path.join(d2, "f2.jpg"), "wb").write(self.photo)           # exact duplicate of the first
        open(os.path.join(d1, "f3.docx"), "wb").write(os.urandom(3000))
        open(os.path.join(d1, "f4.mp4"), "wb").write(os.urandom(9000))
        open(os.path.join(d1, "f5.xyz"), "wb").write(os.urandom(2000))
        open(os.path.join(d1, "f6.txt"), "wb").write(b"tiny")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_files_are_sorted_and_nothing_is_lost(self):
        before = sum(len(f) for _d, _s, f in os.walk(self.tmp))
        summary = self.tidy.organize(self.tmp)
        after = sum(len(f) for _d, _s, f in os.walk(self.tmp))
        self.assertEqual(before, after)
        self.assertEqual(summary["groups"], {"Pictures": 2, "Documents": 1, "Videos": 1, "Other": 1})
        self.assertEqual((summary["tiny"], summary["duplicates"]), (1, 1))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "Pictures", "jpg", "f1.jpg")))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "Pictures", "jpg", "f1_1.jpg")))  # the name clash kept both
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "_duplicates", "f2.jpg")))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "_tiny-scraps", "f6.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "recup_dir.1")))

    def test_running_it_twice_changes_nothing_more(self):
        self.tidy.organize(self.tmp)
        again = self.tidy.organize(self.tmp)
        self.assertEqual(again["duplicates"], 0)
        self.assertEqual(again["tiny"], 0)
