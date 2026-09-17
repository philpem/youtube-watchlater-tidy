from __future__ import annotations

import unittest

from youtube_watchlater_tidy.playlist_playwright import (
    _playlist_id_from_href,
    _video_id_from_href,
)


class PlaylistPlaywrightHelperTests(unittest.TestCase):
    def test_playlist_id_is_parsed_from_relative_and_absolute_urls(self) -> None:
        self.assertEqual(_playlist_id_from_href('/playlist?list=PL123'), 'PL123')
        self.assertEqual(
            _playlist_id_from_href('https://www.youtube.com/playlist?list=PL456&si=abc'),
            'PL456',
        )
        self.assertIsNone(_playlist_id_from_href('/watch?v=abc'))
        self.assertIsNone(_playlist_id_from_href(None))

    def test_video_id_is_parsed_exactly(self) -> None:
        self.assertEqual(_video_id_from_href('/watch?v=abc_DEF-12&list=PL123'), 'abc_DEF-12')
        self.assertEqual(
            _video_id_from_href('https://www.youtube.com/watch?list=PL123&v=zquMVVCnmuk'),
            'zquMVVCnmuk',
        )
        self.assertIsNone(_video_id_from_href('/playlist?list=PL123'))


if __name__ == '__main__':
    unittest.main()
