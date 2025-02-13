import os
import re
import subprocess
import threading
import asyncio
import aiohttp
import logging
import time
from yt_dlp import YoutubeDL

# 設定 logging
logging.basicConfig(level=logging.INFO)


def is_valid_youtube_url(url):
    """檢查是否為合法的 https://youtu.be/ 格式的 YouTube 網址"""
    pattern = r"^https://youtu\.be/[a-zA-Z0-9_-]{11}$"
    return re.match(pattern, url) is not None


def get_user_input():
    """持續接收用戶輸入，直到輸入合法的 YouTube 網址"""
    while True:
        url = input("請輸入 YouTube 網址 (https://youtu.be/): ").strip()
        if is_valid_youtube_url(url):
            return url
        logging.info("無效的網址，請重新輸入。")


def download_with_yt_dlp(url, format_code, output_template):
    """使用 yt-dlp 和 aria2 下載指定格式的影片或音訊"""
    command = [
        "yt-dlp",
        "-f", format_code,
        "--external-downloader", "aria2c",
        "--external-downloader-args", "aria2c:-s 16 -x 16",
        "-o", output_template,
        url
    ]
    subprocess.run(command, check=True)


def check_file_integrity(filename):
    """檢查檔案是否存在且非空"""
    return os.path.exists(filename) and os.path.getsize(filename) > 0


def extract_base_filename(filename):
    """從檔案名中提取 xxxxx 部分（去除 [ID].ext）"""
    match = re.match(r"^(.*?)\s*\[.*?\]\..*$", filename)
    if match:
        return match.group(1).strip()
    return filename


def merge_with_ffmpeg(video_file, audio_file, subtitle_files, output_file, subtitle_langs=None):
    """使用 ffmpeg 混流影片、音訊和多個字幕，並為每個字幕檔案設定語言標籤"""

    if not os.path.exists(video_file) or not os.path.exists(audio_file):
        logging.error("Video or audio file does not exist.")
        return

    command = [
        "ffmpeg",
        "-i", video_file,
        "-i", audio_file
    ]

    subtitle_inputs = []
    for subtitle_file in subtitle_files:
        if subtitle_file and os.path.exists(subtitle_file):
            subtitle_inputs.append(subtitle_file)
            logging.info(f"Merging with subtitle file: {subtitle_file}")
            command.extend(["-i", subtitle_file])
        if subtitle_langs and len(subtitle_langs) == len(subtitle_inputs):
            for i, lang in enumerate(subtitle_langs):
                command.extend(["-metadata:s:s:" + str(i), f"language={lang}"])

    # 添加 -map 指定所有流
    command.extend([
        "-map", "0:v",  # 視頻來自第一個輸入
        "-map", "1:a"   # 音訊來自第二個輸入
    ])

    # 設定字幕流的 -map 和 metadata
    if subtitle_inputs:
        for i in range(len(subtitle_inputs)):
            command.extend(["-map", f"{i+2}:s"])  # 字幕從第 3 個輸入開始 (索引+2)

    # 設置編解碼器
    command.extend([
        "-c:v", "copy",
        "-c:a", "copy",
        "-c:s", "webvtt",
        output_file + ".mkv"
    ])

    # 顯示完整的 ffmpeg 命令
    print("執行命令:", ' '.join(command))

    try:
        subprocess.run(command, shell=False, check=True)
    except subprocess.CalledProcessError as e:
        logging.error(f"ffmpeg 混流失敗: {e}")
        return

    # 檢查混流是否成功
    output_path = output_file + ".mkv"
    if check_file_integrity(output_path):
        logging.info(f"混流完成，輸出檔案: {output_path}")

        # 刪除原始下載的影片、音訊和字幕檔案
        files_to_clean = [video_file, audio_file] + subtitle_files
        for file in files_to_clean:
            if file and os.path.exists(file):
                os.remove(file)
                logging.info(f"已刪除檔案: {file}")
    else:
        logging.error(f"輸出檔案 {output_path} 檢查失敗，可能混流出錯")


def download_video(url):
    """下載影片（格式 616）"""
    download_with_yt_dlp(url, "616", "%(title)s [%(id)s].%(ext)s")


def download_audio(url):
    """下載音訊（格式 140）"""
    download_with_yt_dlp(url, "140", "%(title)s [%(id)s].%(ext)s")


def download_available_subtitles(video_url):
    """下載 yt-dlp --list-subs 中列出的可用字幕（不包含自動生成的字幕）"""
    ydl_opts = {
        'skip_download': True,
        'writesubtitles': True,
        'writeautomaticsub': False,  # 不包含自動生成的字幕
        'subtitlesformat': 'vtt',
        'subtitleslangs': ['all'],  # 下載所有可用的字幕
        'outtmpl': '%(title)s [%(id)s].%(ext)s',
    }

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
            available_subs = info.get('subtitles', {})
            if not available_subs:
                logging.info("沒有可用的字幕。")
                return []

            # 下載所有可用的字幕
            ydl.download([video_url])
            logging.info("所有可用字幕下載完成。")
            return list(available_subs.keys())  # 返回下載的字幕語言列表
    except Exception as e:
        logging.info(f"下載字幕時發生錯誤: {e}")
        return []


async def translate_text(session, text, target_lang='zh-TW'):
    """使用異步方式翻譯文本"""
    url = f'https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl={target_lang}&dt=t&q={text}'
    async with session.get(url) as response:
        result = await response.json()
        return result[0][0][0]


def parse_vtt(subtitle_content):
    """解析 VTT 字幕文件"""
    entries = []
    subtitle_lines = subtitle_content.split('\n')
    i = 0
    while i < len(subtitle_lines):
        if re.match(r'^\d{2}:\d{2}:\d{2}', subtitle_lines[i]):
            start_time, end_time = subtitle_lines[i].split(' --> ')
            text = '\n'.join(subtitle_lines[i + 1:i + 3]).strip()
            entries.append({
                'start_time': start_time,
                'end_time': end_time,
                'text': text
            })
            i += 3
        else:
            i += 1
    return entries


def combine_srt(entries, translated_texts):
    """將翻譯後的文本與時間戳重新組合成 SRT 格式"""
    result = []
    for i, entry in enumerate(entries):
        result.append(f"{i + 1}")
        result.append(
            f"{entry['start_time'].replace('.', ',')} --> {entry['end_time'].replace('.', ',')}")
        result.append(translated_texts[i])
        result.append("")
    return '\n'.join(result)


async def translate_subtitles_parallel(subtitle_file, target_lang):
    """並行翻譯字幕並保存為 vtt 文件，文件名加上 zh_TW"""
    try:
        with open(subtitle_file, 'r', encoding='utf-8') as f:
            subtitle_content = f.read()
        if not subtitle_content.strip():
            logging.info("字幕檔案內容為空，翻譯無法進行。")
            return None
        subtitle_entries = parse_vtt(subtitle_content)
        async with aiohttp.ClientSession() as session:
            tasks = []
            for entry in subtitle_entries:
                tasks.append(translate_text(
                    session, entry['text'], target_lang))
            translated_texts = await asyncio.gather(*tasks)
            translated_subtitles = combine_srt(
                subtitle_entries, translated_texts)

            # 使用原始字幕名稱並加上 zh_TW
            translated_file = f"{subtitle_file.replace('.vtt', '')[:-2]}tw.vtt"
            with open(translated_file, 'w', encoding='utf-8') as f:
                f.write(translated_subtitles)
            logging.info(f"字幕已翻譯並儲存為: {translated_file}")

            return translated_file
    except Exception as e:
        logging.info(f"字幕翻譯時發生錯誤: {e}")
        return None


async def main():
    # 獲取合法的 YouTube 網址
    youtube_url = get_user_input()

    # 下載所有可用的字幕
    downloaded_langs = download_available_subtitles(youtube_url)
    subtitle_files = [f for f in os.listdir() if f.endswith('.vtt')]
    logging.info(f"下載的字幕檔案: {subtitle_files}")

    # 檢查是否有繁體中文字幕 (zh-TW)
    has_zh_tw = any('zh-TW' in f for f in subtitle_files)

    # 如果沒有繁體中文字幕，則進行翻譯
    translated_subtitle_file = None
    if not has_zh_tw:
        # 優先順序：zh > en > ja
        for lang in ['zh', 'en', 'ja']:
            target_subtitle = next(
                (f for f in subtitle_files if lang in f), None)
            if target_subtitle:
                logging.info(f"開始翻譯 {target_subtitle} 為繁體中文...")
                translated_subtitle_file = await translate_subtitles_parallel(target_subtitle, 'zh-TW')
                if translated_subtitle_file:
                    break

    # 同時下載影片和音訊
    logging.info("正在下載影片和音訊...")
    video_thread = threading.Thread(target=download_video, args=(youtube_url,))
    audio_thread = threading.Thread(target=download_audio, args=(youtube_url,))
    video_thread.start()
    audio_thread.start()
    video_thread.join()
    audio_thread.join()

    # 獲取下載的檔案名
    downloaded_files = [f for f in os.listdir() if re.search(r"\[.*?\]\.", f)]
    video_file = next((f for f in downloaded_files if f.endswith(
        ".mp4") or f.endswith(".mkv")), None)
    audio_file = next(
        (f for f in downloaded_files if f.endswith(".m4a")), None)

    # 驗證檔案完整性
    if not all(check_file_integrity(f) for f in [video_file, audio_file] + subtitle_files):
        logging.info("檔案不完整，程式結束。")
        return

    # 混流影片、音訊和字幕
    base_filename = extract_base_filename(video_file)
    output_file = f"{base_filename}"  # 輸出容器改為 MKV
    logging.info("正在混流影片、音訊和字幕...")

    # 如果有翻譯的字幕，加入混流
    if translated_subtitle_file:
        subtitle_files.append(translated_subtitle_file)

    try:
        # 設定字幕語言標籤
        subtitle_langs = []
        for f in subtitle_files:
            if 'zh-TW' in f:
                subtitle_langs.append('zh-TW')
            elif 'zh' in f:
                subtitle_langs.append('zh')
            elif 'en' in f:
                subtitle_langs.append('en')
            elif 'ja' in f:
                subtitle_langs.append('ja')
            else:
                subtitle_langs.append('und')  # 未定義語言

        merge_with_ffmpeg(video_file, audio_file,
                          subtitle_files, output_file, subtitle_langs)
    except Exception as e:
        logging.info(f"混流失敗: {e}")
        return

if __name__ == "__main__":
    asyncio.run(main())
