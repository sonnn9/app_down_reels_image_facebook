# FB Downloader

App GUI tải **ảnh** và **Reels/Video** từ Facebook. Đóng gói thành 1 file `FB_Downloader.exe` chạy độc lập, không cần cài Python trên máy đích.

- Ảnh: dùng [gallery-dl](https://github.com/mikf/gallery-dl) (bundled)
- Reels/Video: dùng [yt-dlp](https://github.com/yt-dlp/yt-dlp) (bundled)

---

## Dùng app (sau khi đã có exe)

1. **Copy file `dist\FB_Downloader.exe`** ra thư mục bất kỳ (vd `D:\FB\`).
2. **Tạo `cookies.txt`** cạnh file exe:
   - Cài extension [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc) cho Chrome/Edge.
   - Đăng nhập https://www.facebook.com.
   - Click icon extension → **Export** → format **Netscape** → lưu file tên `cookies.txt` cạnh exe.
3. **Chạy `FB_Downloader.exe`**.
4. Trong app:
   - Tab **Ảnh**: dán link FB (ảnh/album/profile/post), mỗi dòng 1 link → bấm **Bắt đầu tải**.
   - Tab **Reels / Video**: dán link reels/video → chọn **Tải gì?** (Chỉ video / Chỉ MP3 / Cả hai) → bấm **Bắt đầu tải**. Tên file đặt theo **title** của video.
   - Tab **Cài đặt**: đổi thư mục lưu, đường dẫn `cookies.txt`, bật tùy chọn tải video kèm ảnh.
   - Bấm **Mở thư mục tải về** để xem kết quả.

Link và cài đặt được tự lưu vào `fb_downloader_settings.json` cạnh exe, lần sau mở lại không phải dán lại.

> **Tải MP3 cần ffmpeg.** Chế độ *Chỉ MP3* và *Cả hai* dùng ffmpeg để tách/chuyển âm thanh. App tự dò `ffmpeg.exe` (và `ffprobe.exe`) theo thứ tự: cạnh file exe → thư mục `bin/` cạnh exe → `PATH` của máy. Nếu máy đích chưa có ffmpeg, tải bản Windows từ https://www.gyan.dev/ffmpeg/builds/ rồi copy `ffmpeg.exe` + `ffprobe.exe` vào cạnh `FB_Downloader.exe`. Chế độ *Chỉ video* không cần ffmpeg.

### Tách script (Whisper)

Trong tab **Reels / Video**, tick **"Tách lời thoại ra .srt bằng Whisper turbo"** để sau khi tải xong, app tự chạy [OpenAI Whisper](https://github.com/openai/whisper) (model `turbo` = `large-v3-turbo`) tách lời thoại thành file phụ đề `.srt` cùng tên với file media.

- Chọn **Ngôn ngữ** nếu biết trước (vd `vi`, `en`); để trống = tự động nhận diện.
- **Lần đầu** dùng sẽ tải model `turbo` (~1.5 GB) về cache (`%USERPROFILE%\.cache\whisper`), cần mạng. Các lần sau dùng lại cache, không tải nữa.
- Whisper cũng cần **ffmpeg** để đọc audio (đã nói ở trên).
- Chạy trên CPU (bản đóng gói không kèm CUDA) nên clip dài sẽ mất thời gian.

### Định dạng link hỗ trợ

- Ảnh đơn: `https://www.facebook.com/photo.php?fbid=...`
- Album: `https://www.facebook.com/media/set/?set=...`
- Profile / Page (toàn bộ ảnh public): `https://www.facebook.com/<username>/photos`
- Post: `https://www.facebook.com/<username>/posts/<id>`
- Reels: `https://www.facebook.com/reel/<id>` hoặc `https://www.facebook.com/<user>/videos/<id>`

Dòng bắt đầu bằng `#` được coi là comment.

---

## Build lại exe

Yêu cầu: Python 3.10+ (mặc định script tìm `C:\Python313\python.exe`).

```powershell
.\build.bat
```

Script này sẽ:
1. `pip install -U -r requirements.txt` (gallery-dl, yt-dlp, pyinstaller).
2. Xoá build cũ.
3. Build `dist\FB_Downloader.exe` (one-file, no console).

---

## Cấu trúc dự án

```
.
├── src\fb_downloader.py     # GUI + logic
├── gallery-dl.conf          # config gallery-dl mặc định (bundle vào exe)
├── requirements.txt
├── build.bat                # build exe
├── README.md
└── (sau khi build)
    └── dist\FB_Downloader.exe
```

### File user data (không commit lên git)

- `cookies.txt` — cookies FB của bạn.
- `fb_downloader_settings.json` — link + cài đặt đã lưu.
- `downloads/` — file tải về.

---

## Troubleshooting

- **`HttpError: 401 / 403`** → cookies hết hạn, export lại `cookies.txt`.
- **`No suitable extractor`** → link không đúng định dạng. Mở link trong browser, copy URL thật rồi dán lại.
- **App không mở được** → chạy thử bằng Python xem có lỗi gì:
  ```powershell
  C:\Python313\python.exe src\fb_downloader.py
  ```
- **Bị Facebook chặn rate-limit** → tăng `sleep` trong `gallery-dl.conf` (vd `[3, 6]`).

---

## Legacy

Các file `download.ps1`, `download_image.bat`, `download_reels.bat`, `yt-dlp.exe` của phiên bản cũ vẫn để lại để bạn xem tham khảo hoặc fallback. Sau khi xác nhận exe mới chạy ổn có thể xoá đi.
