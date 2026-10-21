from pathlib import Path
from datetime import datetime
import re
import json
import math
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import urllib.parse
import urllib.request
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageOps, ImageFile

# 일부 데이터가 누락된 사진도 가능한 범위에서 읽습니다.
ImageFile.LOAD_TRUNCATED_IMAGES = True

WIDTH, HEIGHT, FPS = 1920, 1080, 30
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Cancelled(Exception):
    pass


def check_cancel(event):
    if event.is_set():
        raise Cancelled()


def capture_date(path):
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            value = exif.get(36867) or exif.get(306)
            if value:
                return datetime.strptime(str(value), "%Y:%m:%d %H:%M:%S").timestamp()
    except Exception:
        pass
    return path.stat().st_mtime


def folder_date(path):
    """폴더명 맨 앞의 [YYYY-MM-DD]를 읽습니다."""
    match = re.match(
        r"^\[(\d{4}-\d{2}-\d{2})\]",
        path.name
    )
    if not match:
        return None

    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d")
    except ValueError:
        return None


def collect_photo_groups(folder):
    """
    폴더별 사진을 수집합니다.
    중첩 폴더에 날짜가 없으면 상위 행사 폴더의 날짜를 사용합니다.
    """
    root = Path(folder)
    groups = []

    for current, directories, filenames in os.walk(
        root, followlinks=False
    ):
        current_path = Path(current)

        # 링크된 폴더를 따라가거나 같은 사진을 중복 수집하지 않습니다.
        directories[:] = sorted(
            [
                name for name in directories
                if not (current_path / name).is_symlink()
            ],
            key=str.casefold,
        )

        photos = [
            current_path / name
            for name in filenames
            if Path(name).suffix.lower() in IMAGE_EXTENSIONS
            and (current_path / name).is_file()
        ]
        if not photos:
            continue

        # 현재 폴더부터 위로 올라가며 가장 가까운 행사 날짜를 찾습니다.
        event_date = None
        event_folder = root
        candidate = current_path

        while candidate != root:
            date_value = folder_date(candidate)
            if date_value is not None:
                event_date = date_value
                event_folder = candidate
                break
            candidate = candidate.parent

        groups.append(
            (event_date, event_folder, current_path, photos)
        )

    return groups


def find_photos(folder, order):
    root = Path(folder)
    groups = collect_photo_groups(root)

    # 날짜순 → 같은 날짜의 행사 폴더명 → 내부 폴더명.
    # 날짜가 없는 폴더는 마지막에 배치합니다.
    groups.sort(
        key=lambda group: (
            group[0] is None,
            group[0] or datetime.max,
            group[1].relative_to(root).as_posix().casefold(),
            group[2].relative_to(root).as_posix().casefold(),
        )
    )

    result = []

    for _, _, _, photos in groups:
        if order == "촬영 날짜":
            photos.sort(
                key=lambda path: (
                    capture_date(path),
                    path.name.casefold(),
                )
            )
        else:
            photos.sort(key=lambda path: path.name.casefold())

        result.extend(photos)

    return result



def prepare_photo(source, destination):
    # EXIF 회전을 반영하고 영상에 필요한 크기로 줄입니다.
    with Image.open(source) as original:
        original.draft("RGB", (3840, 2160))
        image = ImageOps.exif_transpose(original)
        image.thumbnail((3840, 2160), Image.Resampling.LANCZOS)

        if image.mode in ("RGBA", "LA") or "transparency" in image.info:
            rgba = image.convert("RGBA")
            base = Image.new("RGB", rgba.size, "white")
            base.paste(rgba, mask=rgba.getchannel("A"))
            image = base
        else:
            image = image.convert("RGB")

        image.save(destination, quality=94)


def read_json(url, cancel):
    check_cancel(cancel)
    request = urllib.request.Request(
        url, headers={"User-Agent": "PhotoVideoTool/1.0"}
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        data = response.read(4 * 1024 * 1024 + 1)
    if len(data) > 4 * 1024 * 1024:
        raise RuntimeError("음악 검색 응답이 너무 큽니다.")
    check_cancel(cancel)
    return json.loads(data)


def automatic_music(workdir, cancel, log):
    log("Internet Archive에서 CC0 배경음악을 검색합니다.")
    query = (
        'mediatype:audio AND (ambient OR instrumental) '
        'AND licenseurl:"https://creativecommons.org/publicdomain/zero/1.0/"'
    )
    params = urllib.parse.urlencode({
        "q": query,
        "fl[]": "identifier",
        "rows": 12,
        "output": "json",
    })
    result = read_json(
        "https://archive.org/advancedsearch.php?" + params, cancel
    )

    for item in result.get("response", {}).get("docs", []):
        check_cancel(cancel)
        identifier = item.get("identifier")
        if not identifier:
            continue

        try:
            metadata = read_json(
                "https://archive.org/metadata/"
                + urllib.parse.quote(identifier, safe=""),
                cancel,
            )
            license_value = metadata.get("metadata", {}).get("licenseurl", [])
            licenses = (
                license_value if isinstance(license_value, list)
                else [license_value]
            )
            if not any(
                str(value).replace("http://", "https://").rstrip("/")
                == "https://creativecommons.org/publicdomain/zero/1.0"
                for value in licenses
            ):
                continue

            candidates = []
            for entry in metadata.get("files", []):
                name = entry.get("name", "")
                try:
                    size = int(entry.get("size", 0))
                except (TypeError, ValueError):
                    continue
                if name.lower().endswith(".mp3") and 0 < size <= 50_000_000:
                    candidates.append((size, name))

            if not candidates:
                continue

            _, filename = min(candidates)
            url = (
                "https://archive.org/download/"
                + urllib.parse.quote(identifier, safe="")
                + "/" + urllib.parse.quote(filename, safe="/")
            )
            target = Path(workdir) / "automatic_music.mp3"
            request = urllib.request.Request(
                url, headers={"User-Agent": "PhotoVideoTool/1.0"}
            )
            downloaded = 0
            with urllib.request.urlopen(request, timeout=20) as response:
                with target.open("wb") as output:
                    while True:
                        check_cancel(cancel)
                        block = response.read(128 * 1024)
                        if not block:
                            break
                        downloaded += len(block)
                        if downloaded > 50_000_000:
                            raise RuntimeError("음원 다운로드 용량 제한을 넘었습니다.")
                        output.write(block)

            source = (
                f"Source: https://archive.org/details/{identifier}\n"
                f"File: {filename}\n"
                "Declared license: CC0 1.0\n"
                "https://creativecommons.org/publicdomain/zero/1.0/\n"
                "License information is supplied by the archive uploader.\n"
            )
            return target, source
        except Cancelled:
            raise
        except Exception:
            continue

    raise RuntimeError(
        "자동 음악을 다운로드하지 못했습니다.\n"
        "직접 음악 파일을 선택하거나 음악 없이 다시 실행하세요."
    )


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("사진 → MP4")
        self.root.geometry("730x620")
        self.events = queue.Queue()
        self.cancel_event = threading.Event()
        self.running = False

        self.folder = tk.StringVar()
        self.output = tk.StringVar()
        self.order = tk.StringVar(value="파일명")
        self.seconds = tk.StringVar(value="4")
        self.music_mode = tk.StringVar(value="자동 검색")
        self.music_file = tk.StringVar()
        self.estimate = tk.StringVar(value="사진 폴더를 선택하세요.")

        frame = ttk.Frame(root, padding=16)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        self.path_row(frame, 0, "사진 폴더", self.folder, self.choose_folder)
        self.path_row(frame, 1, "MP4 저장 위치", self.output, self.choose_output)

        ttk.Label(frame, text="정렬").grid(row=2, column=0, sticky="w", pady=7)
        sort_box = ttk.Combobox(
            frame, textvariable=self.order,
            values=["파일명", "촬영 날짜"], state="readonly"
        )
        sort_box.grid(row=2, column=1, sticky="ew")
        sort_box.bind("<<ComboboxSelected>>", lambda _: self.update_estimate())

        ttk.Label(frame, text="사진당 시간(초)").grid(
            row=3, column=0, sticky="w", pady=7
        )
        ttk.Entry(frame, textvariable=self.seconds).grid(
            row=3, column=1, sticky="ew"
        )
        self.seconds.trace_add("write", lambda *_: self.update_estimate())

        ttk.Label(frame, textvariable=self.estimate).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=8
        )
        ttk.Label(
            frame,
            text="촬영 날짜가 없으면 파일 수정 날짜를 사용합니다.\n"
                 "페이드는 검정 화면을 거쳐 전환하며, 표시 시간에 포함됩니다.",
        ).grid(row=5, column=0, columnspan=3, sticky="w")

        ttk.Label(frame, text="배경음악").grid(
            row=6, column=0, sticky="w", pady=10
        )
        ttk.Combobox(
            frame, textvariable=self.music_mode,
            values=["자동 검색", "직접 선택", "없음"], state="readonly"
        ).grid(row=6, column=1, sticky="ew")

        self.path_row(
            frame, 7, "음악 파일", self.music_file, self.choose_music
        )

        buttons = ttk.Frame(frame)
        buttons.grid(row=8, column=0, columnspan=3, sticky="w", pady=14)
        self.preview_button = ttk.Button(
            buttons, text="10장 미리보기", command=lambda: self.start(True)
        )
        self.preview_button.pack(side="left", padx=(0, 8))
        self.render_button = ttk.Button(
            buttons, text="전체 영상 만들기", command=lambda: self.start(False)
        )
        self.render_button.pack(side="left", padx=(0, 8))
        self.cancel_button = ttk.Button(
            buttons, text="취소", command=self.cancel, state="disabled"
        )
        self.cancel_button.pack(side="left")

        self.progress = ttk.Progressbar(frame, maximum=100)
        self.progress.grid(row=9, column=0, columnspan=3, sticky="ew")

        self.log_box = tk.Text(frame, height=12, wrap="word", state="disabled")
        self.log_box.grid(
            row=10, column=0, columnspan=3, sticky="nsew", pady=(10, 0)
        )
        frame.rowconfigure(10, weight=1)

        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)

    def path_row(self, parent, row, label, variable, command):
        ttk.Label(parent, text=label).grid(
            row=row, column=0, sticky="w", padx=(0, 10), pady=7
        )
        ttk.Entry(parent, textvariable=variable).grid(
            row=row, column=1, sticky="ew"
        )
        ttk.Button(parent, text="찾기", command=command).grid(
            row=row, column=2, padx=(8, 0)
        )

    def choose_folder(self):
        selected = filedialog.askdirectory()
        if selected:
            self.folder.set(selected)
            self.update_estimate()

    def choose_output(self):
        selected = filedialog.asksaveasfilename(
            defaultextension=".mp4",
            filetypes=[("MP4 영상", "*.mp4")],
        )
        if selected:
            self.output.set(selected)

    def choose_music(self):
        selected = filedialog.askopenfilename(
            filetypes=[
                ("음악", "*.mp3 *.wav *.m4a *.flac *.ogg"),
                ("모든 파일", "*.*"),
            ]
        )
        if selected:
            self.music_file.set(selected)
            self.music_mode.set("직접 선택")

    def update_estimate(self):
        try:
            seconds = float(self.seconds.get())
            if not math.isfinite(seconds) or not 1 <= seconds <= 60:
                raise ValueError()
            folder = Path(self.folder.get())
            if not self.folder.get() or not folder.is_dir():
                raise ValueError()
            count = sum(
                len(group[3])
                for group in collect_photo_groups(folder)
            )

            # 영상 프레임 단위로 반올림한 실제 시간을 사용합니다.
            duration = count * round(seconds * FPS) / FPS
            self.estimate.set(
                f"사진 {count}장 · 예상 길이 "
                f"{int(duration // 60)}분 {duration % 60:.1f}초"
            )
        except (ValueError, OSError):
            self.estimate.set("폴더와 표시 시간(1~60초)을 확인하세요.")

    def log(self, text):
        self.events.put(("log", text))

    def start(self, preview):
        if self.running:
            return
        try:
            if not shutil.which("ffmpeg"):
                raise ValueError("FFmpeg를 설치하고 Path에 추가하세요.")
            folder = Path(self.folder.get())
            if not self.folder.get() or not folder.is_dir():
                raise ValueError("사진 폴더를 선택하세요.")
            seconds = float(self.seconds.get())
            if not math.isfinite(seconds) or not 1 <= seconds <= 60:
                raise ValueError("사진당 시간은 1~60초로 입력하세요.")
            if not self.output.get():
                raise ValueError("MP4 저장 위치를 선택하세요.")
            output = Path(self.output.get()).resolve()
            if output.suffix.lower() != ".mp4":
                raise ValueError("저장 파일 확장자는 .mp4여야 합니다.")
            if not output.parent.is_dir():
                raise ValueError("저장할 폴더가 존재하지 않습니다.")
            if preview:
                output = output.with_name(output.stem + "_preview.mp4")
            if output.exists():
                raise ValueError(
                    f"같은 이름의 파일이 있습니다. 다른 이름을 선택하세요.\n{output}"
                )
            music_mode = self.music_mode.get()
            music_file = self.music_file.get()
            if music_mode == "직접 선택" and not Path(music_file).is_file():
                raise ValueError("음악 파일을 선택하세요.")
        except (ValueError, OSError) as error:
            messagebox.showerror("입력 확인", str(error))
            return

        self.running = True
        self.cancel_event.clear()
        self.progress["value"] = 0
        self.preview_button["state"] = "disabled"
        self.render_button["state"] = "disabled"
        self.cancel_button["state"] = "normal"

        arguments = (
            folder, output, self.order.get(), seconds,
            music_mode, music_file, preview
        )
        threading.Thread(
            target=self.worker, args=arguments, daemon=True
        ).start()

    def run_ffmpeg(self, arguments, workdir, duration, start, span):
        check_cancel(self.cancel_event)
        error_file = Path(workdir) / "ffmpeg_error.log"
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-nostats", "-progress", "pipe:1", "-y",
        ] + arguments

        with error_file.open("w", encoding="utf-8") as errors:
            process = subprocess.Popen(
                command,
                cwd=workdir,
                stdout=subprocess.PIPE,
                stderr=errors,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=CREATE_NO_WINDOW,
            )

            # stdout에 새 출력이 없어도 취소할 수 있도록 별도로 감시합니다.
            finished = threading.Event()

            def watch_cancel():
                while not finished.wait(0.2):
                    if self.cancel_event.is_set():
                        try:
                            process.kill()
                        except OSError:
                            pass
                        return

            watcher = threading.Thread(target=watch_cancel, daemon=True)
            watcher.start()
            try:
                for line in process.stdout:
                    if line.startswith("out_time_us="):
                        try:
                            elapsed = int(line.split("=", 1)[1]) / 1_000_000
                            fraction = min(1, elapsed / max(duration, 0.001))
                            self.events.put(
                                ("progress", start + span * fraction)
                            )
                        except ValueError:
                            pass
                code = process.wait()
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
                process.stdout.close()
                finished.set()
                watcher.join()

        check_cancel(self.cancel_event)
        if code:
            details = error_file.read_text(
                encoding="utf-8", errors="replace"
            )[-5000:]
            raise RuntimeError("FFmpeg 처리 실패:\n" + details)

    def worker(
        self, folder, output, order, seconds,
        music_mode, music_file, preview
    ):
        try:
            self.log("사진 목록을 읽고 정렬합니다.")
            photos = find_photos(folder, order)
            check_cancel(self.cancel_event)
            if preview:
                photos = photos[:10]
            if not photos:
                raise RuntimeError("JPG, JPEG, PNG 사진이 없습니다.")

            self.log("전체 사진의 손상 여부를 확인합니다.")
            readable_photos = []

            for index, photo in enumerate(photos):
                check_cancel(self.cancel_event)

                try:
                    with Image.open(photo) as image:
                        image.load()

                    readable_photos.append(photo)

                except (OSError, ValueError, SyntaxError) as error:
                    self.log(
                        f"읽을 수 없어 제외한 사진: {photo}\n"
                        f"  원인: {error}"
                    )

                self.events.put((
                    "progress",
                    5 * (index + 1) / len(photos)
                ))

            skipped = len(photos) - len(readable_photos)
            photos = readable_photos

            if not photos:
                raise RuntimeError(
                    "읽을 수 있는 사진이 없습니다. 원본 사진을 확인하세요."
                )

            self.log(
                f"검사 완료: 사용 {len(photos)}장, 제외 {skipped}장"
            )
            
            frames = round(seconds * FPS)
            clip_duration = frames / FPS
            total_duration = len(photos) * clip_duration
            fade = min(0.5, clip_duration / 4)

            # 출력 폴더 안에서만 임시 작업하며 완료/취소 후 자동 정리합니다.
            with tempfile.TemporaryDirectory(
                prefix=".photo_video_", dir=output.parent
            ) as temporary:
                workdir = Path(temporary)
                music_path = None
                music_source = None

                if music_mode == "자동 검색":
                    try:
                        music_path, music_source = automatic_music(
                            workdir, self.cancel_event, self.log
                        )
                    except Cancelled:
                        raise
                    except Exception as error:
                        music_path = None
                        music_source = None
                        self.log(f"자동 음원 검색 실패: {error}")
                        self.log("배경음악 없이 영상 제작을 계속합니다.")

                elif music_mode == "직접 선택":
                    music_path = Path(music_file).resolve()


                self.events.put(("progress", 5))
                playlist = workdir / "clips.txt"

                with playlist.open("w", encoding="utf-8") as listing:
                    for index, photo in enumerate(photos):
                        check_cancel(self.cancel_event)
                        self.log(
                            f"사진 {index + 1}/{len(photos)}: {photo.name}"
                        )
                        prepared = workdir / "current.jpg"
                        prepare_photo(photo, prepared)
                        clip_name = f"clip_{index:05d}.mp4"

                        # 앞 사진은 화면 안에 맞춘 후 이동하므로 잘리지 않습니다.
                        # 검정 패딩은 overlay 단계에서 투명하게 처리합니다.
                        pan_x = (
                            f"(iw-iw/zoom)*on/{max(frames - 1, 1)}"
                            if index % 2 == 0 else
                            f"(iw-iw/zoom)*(1-on/{max(frames - 1, 1)})"
                        )
                        filters = (
                            "[0:v]split=2[bg][fg];"
                            "[bg]scale=1920:1080:"
                            "force_original_aspect_ratio=increase,"
                            "crop=1920:1080,boxblur=24:2,"
                            "setsar=1[back];"
                            "[fg]scale=1720:960:"
                            "force_original_aspect_ratio=decrease,"
                            "format=rgba,"
                            "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:"
                            "color=black@0,"
                            "setsar=1[front];"
                            "[back][front]overlay=0:0:format=auto,"
                            "format=yuv420p,"
                            # 정지 사진을 지정한 프레임 수만큼 반복합니다.
                            f"loop=loop={frames - 1}:size=1:start=0,"
                            f"setpts=N/({FPS}*TB),"
                            # 크게 만든 화면에서 천천히 이동한 뒤 축소합니다.
                            # 확대 비율은 고정하여 확대 과정의 떨림을 줄입니다.
                            "scale=3840:2160:flags=lanczos,"
                            "crop=w=3648:h=2052:"
                            f"x='96+64*sin(PI*t/{clip_duration})"
                            f"*{1 if index % 2 == 0 else -1}':"
                            "y=54,"
                            "scale=1920:1080:flags=lanczos,"
                            f"fade=t=in:st=0:d={fade},"
                            f"fade=t=out:st={clip_duration-fade}:d={fade},"
                            "setsar=1[v]"
                        )
                        self.run_ffmpeg(
                            [
                                "-i", str(prepared),
                                "-filter_complex_threads", "1",
                                "-filter_complex", filters,
                                "-map", "[v]",
                                "-frames:v", str(frames),
                                "-an", "-c:v", "libx264",
                                "-preset", "veryfast", "-crf", "20",
                                "-pix_fmt", "yuv420p",
                                "-threads", "2",
                                clip_name,
                            ],
                            workdir,
                            clip_duration,
                            5 + 85 * index / len(photos),
                            85 / len(photos),
                        )
                        listing.write(f"file '{clip_name}'\n")

                self.log("영상을 합치고 음악을 적용합니다.")
                temporary_output = workdir / "result.mp4"
                arguments = [
                    "-f", "concat", "-safe", "0",
                    "-i", str(playlist),
                ]
                if music_path:
                    audio_fade = min(3, total_duration / 4)
                    arguments += [
                        "-stream_loop", "-1", "-i", str(music_path),
                        "-map", "0:v:0", "-map", "1:a:0",
                        "-c:v", "copy",
                        "-af",
                        f"afade=t=in:st=0:d={audio_fade},"
                        f"afade=t=out:"
                        f"st={total_duration-audio_fade}:d={audio_fade}",
                        "-c:a", "aac", "-b:a", "192k",
                    ]
                else:
                    arguments += ["-map", "0:v:0", "-c:v", "copy", "-an"]

                arguments += [
                    "-t", f"{total_duration:.6f}",
                    "-movflags", "+faststart",
                    str(temporary_output),
                ]
                self.run_ffmpeg(
                    arguments, workdir, total_duration, 90, 10
                )
                check_cancel(self.cancel_event)

                # Windows에서는 기존 파일이 있으면 rename이 실패하여 보호됩니다.
                if output.exists():
                    raise RuntimeError("출력 파일이 이미 생겼습니다. 이름을 바꾸세요.")
                temporary_output.rename(output)

                if music_source:
                    source_file = output.with_suffix(".music.txt")
                    try:
                        with source_file.open("x", encoding="utf-8") as file:
                            file.write(music_source)
                    except OSError as error:
                        self.log(f"음악 출처 파일 저장 실패: {error}")
                        self.log(music_source)

            self.events.put(("done", str(output)))
        except Cancelled:
            self.events.put(("cancelled", "취소했습니다. 원본 사진은 보존됩니다."))
        except Exception as error:
            self.events.put(("error", str(error)))

    def cancel(self):
        if self.running:
            self.cancel_event.set()
            self.cancel_button["state"] = "disabled"
            self.log("취소 중입니다. 네트워크 요청은 시간 제한까지 기다릴 수 있습니다.")

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "progress":
                    self.progress["value"] = value
                elif kind == "log":
                    self.log_box["state"] = "normal"
                    self.log_box.insert("end", value + "\n")
                    self.log_box.see("end")
                    self.log_box["state"] = "disabled"
                elif kind in ("done", "cancelled", "error"):
                    self.running = False
                    self.preview_button["state"] = "normal"
                    self.render_button["state"] = "normal"
                    self.cancel_button["state"] = "disabled"
                    if kind == "done":
                        self.progress["value"] = 100
                        messagebox.showinfo("완료", f"저장했습니다.\n{value}")
                    elif kind == "error":
                        messagebox.showerror("오류", value)
                    else:
                        messagebox.showinfo("취소", value)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def close(self):
        if self.running:
            self.cancel()
            messagebox.showinfo(
                "취소 중", "작업 취소가 끝나면 창을 닫아주세요."
            )
            return
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
