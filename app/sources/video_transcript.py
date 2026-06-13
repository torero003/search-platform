import asyncio
import logging
import os
import re
import tempfile
from typing import Optional

logger = logging.getLogger(__name__)

# Path to Python that has yt-dlp installed
_YT_DLP_PYTHON = os.getenv('YT_DLP_PYTHON', 'D:/tools/anaconda/python.exe')


def _parse_vtt_text(vtt_content: str) -> str:
    """Parse VTT/SRT subtitle content into plain text."""
    lines = vtt_content.split('\n')
    text_lines = []
    in_header = True
    for line in lines:
        line = line.strip()
        if in_header and not line:
            continue  # skip blank lines until first timestamp
        if '-->' in line:
            in_header = False
            continue
        if in_header:
            continue  # skip WEBVTT, Kind:, Language:, etc.
        if re.match(r'^\d+$', line):
            continue
        if line.startswith('position:') or line.startswith('align:'):
            continue
        # Remove timestamp tags like <00:00:01.040> and <c>...</c>
        line = re.sub(r'<[^>]+>', '', line)
        line = line.strip()
        if line:
            text_lines.append(line)
    # VTT has repeated lines (each line is a cumulative update), deduplicate
    unique = []
    for line in text_lines:
        if not unique or line != unique[-1]:
            unique.append(line)
    return ' '.join(unique)


async def fetch_youtube_transcript(video_id: str, max_chars: int = 400) -> Optional[str]:
    """Fetch YouTube video transcript via yt-dlp subprocess.

    Uses yt-dlp to download auto-generated subtitles, then parses the VTT file.

    Args:
        video_id: YouTube video ID (e.g., 's_o8dwzRlu4')
        max_chars: Maximum characters to return

    Returns:
        Transcript text or None
    """
    try:
        video_url = f'https://www.youtube.com/watch?v={video_id}'
        tmpdir = tempfile.mkdtemp()
        output_tpl = os.path.join(tmpdir, '%(id)s.%(lang)s.%(sub_ext)s')

        cmd = [
            _YT_DLP_PYTHON, '-m', 'yt_dlp',
            '--write-sub', '--write-auto-sub',
            '--sub-lang', 'zh,en',
            '--sub-format', 'txt',
            '--skip-download',
            '--no-warnings',
            '--no-check-certificates',
            '--output', output_tpl,
            video_url,
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)

        if proc.returncode != 0:
            err = stderr.decode('utf-8', errors='replace')
            logger.debug(f"yt-dlp failed for {video_id}: {err[:200]}")
            return None

        # Find subtitle files
        files = os.listdir(tmpdir)
        sub_files = [f for f in files if f.endswith(('.vtt', '.srt', '.txt'))]
        if not sub_files:
            logger.debug(f"No subtitle file found for {video_id}")
            return None

        # Prefer Chinese, then English, then any
        sub_files.sort(key=lambda f: (
            0 if 'zh' in f.lower() else 1 if 'en' in f.lower() else 2,
            f
        ))

        for sub_file in sub_files:
            fpath = os.path.join(tmpdir, sub_file)
            try:
                with open(fpath, 'r', encoding='utf-8') as f:
                    content = f.read()
                if not content or len(content) < 20:
                    continue
                text = _parse_vtt_text(content)
                if text and len(text) >= 20:
                    lang = 'en'
                    if 'zh' in sub_file.lower():
                        lang = 'zh'
                    elif 'en' in sub_file.lower():
                        lang = 'en'
                    logger.info(f"YouTube transcript extracted for {video_id} ({lang}, {len(text)} chars)")
                    return text[:max_chars]
            except Exception as e:
                logger.debug(f"Error reading subtitle file {sub_file}: {e}")
                continue

        return None

    except asyncio.TimeoutError:
        logger.debug(f"yt-dlp timeout for {video_id}")
        return None
    except Exception as e:
        logger.debug(f"fetch_youtube_transcript({video_id}): error: {e}")
        return None


async def fetch_bilibili_transcript(video_url: str, cdp=None, max_chars: int = 400) -> Optional[str]:
    """Fetch Bilibili video transcript via CDP browser.

    Bilibili subtitles require browser cookies for API access.
    """
    try:
        from app.sources.cdp_client import _get_cdp

        if cdp is None:
            cdp = await _get_cdp(source_name='bilibili')

        await cdp.send('Page.navigate', {'url': video_url})
        await asyncio.sleep(5)

        js = """(function() {
            try {
                var data = window.__INITIAL_STATE__ || {};
                var videoData = data.videoData || {};
                var cid = videoData.cid;
                if (!cid) return null;
                return 'https://api.bilibili.com/x/v1/dm/subtitle?cid=' + cid;
            } catch(e) {
                return null;
            }
        })()"""

        result = await cdp.send('Runtime.evaluate', {
            'expression': js,
            'returnByValue': True,
        })
        rval = result.get('result', {}).get('result', {})
        subtitle_api = rval.get('value')

        if not subtitle_api:
            return None

        download_js = f"""(async function() {{
            try {{
                var resp = await fetch("{subtitle_api}");
                var json = await resp.json();
                var subs = json.data || [];
                for (var i = 0; i < subs.length; i++) {{
                    var sub = subs[i];
                    if (sub.lan && (sub.lan.indexOf('zh') !== -1 || sub.lan.indexOf('en') !== -1)) {{
                        var subResp = await fetch(sub.subtitle_url);
                        var subJson = await subResp.json();
                        return subJson.body ? subJson.body.map(function(s) {{ return s.content; }}).join(' ') : null;
                    }}
                }}
                if (subs.length > 0) {{
                    var subResp = await fetch(subs[0].subtitle_url);
                    var subJson = await subResp.json();
                    return subJson.body ? subJson.body.map(function(s) {{ return s.content; }}).join(' ') : null;
                }}
                return null;
            }} catch(e) {{
                return null;
            }}
        }})()"""

        result = await cdp.send('Runtime.evaluate', {
            'expression': download_js,
            'awaitPromise': True,
            'returnByValue': True,
        })
        rval = result.get('result', {}).get('result', {})
        content = rval.get('value')

        if not content:
            return None

        text = content.strip()
        if len(text) < 20:
            return None

        return text[:max_chars]

    except Exception as e:
        logger.debug(f"fetch_bilibili_transcript({video_url[:60]}): error: {e}")
        return None


async def enrich_results_with_transcripts(results: list[dict], source: str, max_videos: int = 5):
    """Enrich search results with transcript text.

    For YouTube: uses yt-dlp subprocess to download auto-generated subtitles.
    For Bilibili: uses CDP browser to extract subtitle data from page.

    Args:
        results: List of result dicts with 'url' and 'content' fields
        source: Source name ('youtube' or 'bilibili')
        max_videos: Max number of videos to enrich (default: top 5)
    """
    if source == 'youtube':
        return await _enrich_youtube_transcripts(results, max_videos)
    elif source == 'bilibili':
        return await _enrich_bilibili_transcripts(results, max_videos)


async def _enrich_youtube_transcripts(results: list[dict], max_videos: int):
    """Enrich YouTube results with transcripts using yt-dlp."""
    enriched_count = 0
    total_timeout = 45.0
    start = asyncio.get_event_loop().time()

    for i, r in enumerate(results[:max_videos]):
        elapsed = asyncio.get_event_loop().time() - start
        if elapsed > total_timeout:
            logger.debug(f"YouTube transcript enrichment: timeout after {elapsed:.0f}s")
            break

        url = r.get('url', '')
        video_id_match = re.search(r'[?&]v=([A-Za-z0-9_-]{11})', url)
        if not video_id_match:
            continue

        transcript = await asyncio.wait_for(
            fetch_youtube_transcript(video_id_match.group(1)),
            timeout=15.0,
        )

        if transcript:
            existing = r.get('content', '')
            prefix = ' | Transcript: ' if existing else 'Transcript: '
            r['content'] = existing + prefix + transcript[:400]
            enriched_count += 1
            logger.info(f"YouTube transcript enriched: {transcript[:80]}...")

    if enriched_count:
        logger.info(f"YouTube transcript enriched {enriched_count}/{min(max_videos, len(results))} results")


async def _enrich_bilibili_transcripts(results: list[dict], max_videos: int):
    """Enrich Bilibili results with transcripts using CDP."""
    try:
        from app.sources.cdp_client import _get_cdp
        cdp = await _get_cdp(source_name='bilibili')
    except Exception as e:
        logger.debug(f"enrich_bilibili_transcripts: can't get CDP: {e}")
        return

    enriched_count = 0
    total_timeout = 30.0
    start = asyncio.get_event_loop().time()

    for i, r in enumerate(results[:max_videos]):
        elapsed = asyncio.get_event_loop().time() - start
        if elapsed > total_timeout:
            logger.debug(f"Bilibili transcript enrichment: timeout after {elapsed:.0f}s")
            break

        url = r.get('url', '')
        if not url:
            continue

        try:
            transcript = await asyncio.wait_for(
                fetch_bilibili_transcript(url, cdp=cdp),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            logger.debug(f"Bilibili transcript timeout for {url[:60]}")
            continue
        except Exception as e:
            logger.debug(f"Bilibili transcript error for {url[:60]}: {e}")
            continue

        if transcript:
            existing = r.get('content', '')
            prefix = ' | Transcript: ' if existing else 'Transcript: '
            r['content'] = existing + prefix + transcript[:400]
            enriched_count += 1
            logger.info(f"Bilibili transcript enriched: {transcript[:80]}...")

    if enriched_count:
        logger.info(f"Bilibili transcript enriched {enriched_count}/{min(max_videos, len(results))} results")
