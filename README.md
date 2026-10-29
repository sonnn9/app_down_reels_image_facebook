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
   - Tab **Reels / Video**: dán link reels/video → chọn **Tải gì?** (Chỉ video / Chỉ MP3 / Cả hai) và **Định dạng video** → bấm **Bắt đầu tải**. Tên file đặt theo **title** của video.
     - *TV: H.264 1080p / 720p + M4A (mp4)* (mặc định): file phát thẳng trên TV. Facebook Reels thường chỉ có AV1/VP9 ở 1080p (H.264 chỉ có bản 360p), nên sau khi tải app tự chuyển sang H.264 + AAC bằng ffmpeg (ưu tiên GPU NVIDIA – NVENC, không có thì dùng CPU). File đã là H.264 thì giữ nguyên.
     - *Tốt nhất*: giữ nguyên định dạng gốc (có thể AV1/VP9), không chuyển.
   - Tab **Cài đặt**: đổi thư mục lưu, đường dẫn `cookies.txt`, bật tùy chọn tải video kèm ảnh.
   - Bấm **Mở thư mục tải về** để xem kết quả.

Link và cài đặt được tự lưu vào `fb_downloader_settings.json` cạnh exe, lần sau mở lại không phải dán lại.

> **Tải MP3 cần ffmpeg.** Chế độ *Chỉ MP3* và *Cả hai* dùng ffmpeg để tách/chuyển âm thanh. App tự dò `ffmpeg.exe` (và `ffprobe.exe`) theo thứ tự: cạnh file exe → thư mục `bin/` cạnh exe → `PATH` của máy. Nếu máy đích chưa có ffmpeg, tải bản Windows từ https://www.gyan.dev/ffmpeg/builds/ rồi copy `ffmpeg.exe` + `ffprobe.exe` vào cạnh `FB_Downloader.exe`. Định dạng *TV: H.264* cũng cần ffmpeg (ghép mp4 và chuyển sang H.264); chỉ *Chỉ video* + *Tốt nhất* là không cần.

### Tách script (Whisper)

Trong tab **Reels / Video**, tick **"Tách lời thoại ra .srt bằng Whisper turbo"** để sau khi tải xong, app tự chạy [OpenAI Whisper](https://github.com/openai/whisper) (model `turbo` = `large-v3-turbo`) tách lời thoại thành file phụ đề `.srt` cùng tên với file media.

- Chọn **Ngôn ngữ** nếu biết trước (vd `vi`, `en`); để trống = tự động nhận diện.
- **Lần đầu** dùng sẽ tải model `turbo` (~1.5 GB) về cache (`%USERPROFILE%\.cache\whisper`), cần mạng. Các lần sau dùng lại cache, không tải nữa.
- Whisper cũng cần **ffmpeg** để đọc audio (đã nói ở trên).
- App **tự dùng GPU NVIDIA nếu có**, không có thì rơi về CPU. Log sẽ ghi rõ `[WHISPER] GPU: ...` kèm dung lượng VRAM,
  và cảnh báo nếu model đã chọn không vừa VRAM.

#### Bật GPU

`pip install openai-whisper` chỉ kéo về torch bản **CPU-only**, nên mặc định GPU không hề được dùng. Muốn bật:

```powershell
pip install torch==2.11.0+cu126 --index-url https://download.pytorch.org/whl/cu126
python -c "import torch; print(torch.cuda.is_available())"   # phải in True
```

Dùng `cu126` cho card GTX 10xx (Pascal, `sm_61`) — bản `cu128`/`cu130` đã bỏ hỗ trợ Pascal.
Đổi lại, torch CUDA nặng ~4 GB (bản CPU chỉ 448 MB), nên thư mục `dist/` sau khi build sẽ phồng từ ~700 MB lên **~4 GB**.

#### Chọn model theo VRAM

Điều quyết định tốc độ **không phải** GPU mạnh hay yếu, mà là **model có vừa VRAM hay không**. Vượt VRAM thì phải tràn
sang RAM hệ thống qua PCIe và chậm hẳn. Số đo thật trên GTX 1050 Ti 4 GB + i7-8700, với 120 giây audio tiếng Anh:

| Model | VRAM đỉnh | CPU | GPU | GPU nhanh hơn |
|---|---|---|---|---|
| `small` | 1.4 GB — vừa | 46.7s (2.6x realtime) | **19.1s (6.3x realtime)** | 2.4x |
| `turbo` | 4.6 GB — tràn | 107.7s (1.1x realtime) | 56.4s (2.1x realtime) | 1.9x |
| `medium` | 4.3 GB — tràn | 128.9s (0.9x realtime) | 86.8s (1.4x realtime) | 1.5x |

Nghĩa là trên card 4 GB, `small` chạy GPU nhanh gấp **5.6 lần** `turbo` chạy CPU (file 20 phút: ~3 phút thay vì ~18 phút),
đổi lại độ chính xác thấp hơn `turbo`. Card từ 6 GB trở lên thì `turbo` vừa VRAM và sẽ nhanh hơn hẳn.


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

- **`HTTP Error 403: Forbidden` khi tải YouTube/Reels** → gần như luôn là do `yt-dlp` đã cũ
  (YouTube đổi cơ chế liên tục). Cách sửa nhanh, **không cần build lại app**: tải
  [`yt-dlp.exe` bản mới nhất](https://github.com/yt-dlp/yt-dlp/releases/latest) đặt cạnh file
  `FB_Downloader.exe` (hoặc chạy `yt-dlp.exe -U`). App tự so sánh phiên bản và luôn dùng bản mới hơn
  giữa file rời và bản đóng gói bên trong — dòng `[YT-DLP] Dung: ...` trong log cho biết nó đang dùng bản nào.
- **Log báo `No supported JavaScript runtime could be found`** → cài
  [Deno](https://deno.com) hoặc [Node](https://nodejs.org) (hoặc bỏ `deno.exe`/`node.exe` cạnh app).
  yt-dlp cần JS runtime để giải challenge của YouTube; thiếu nó sẽ mất một số format hoặc dính 403.
  Log sẽ hiện `[JS] Dung JavaScript runtime: ...` khi đã tìm thấy.
- **`HttpError: 401 / 403` khi tải ảnh Facebook** → cookies hết hạn, export lại `cookies.txt`.
- **`No suitable extractor`** → link không đúng định dạng. Mở link trong browser, copy URL thật rồi dán lại.
- **App không mở được** → chạy thử bằng Python xem có lỗi gì:
  ```powershell
  C:\Python313\python.exe src\fb_downloader.py
  ```
- **Bị Facebook chặn rate-limit** → tăng `sleep` trong `gallery-dl.conf` (vd `[3, 6]`).

---

## Legacy

Các file `download.ps1`, `download_image.bat`, `download_reels.bat` của phiên bản cũ vẫn để lại để bạn xem tham khảo hoặc fallback. Sau khi xác nhận exe mới chạy ổn có thể xoá đi.

`yt-dlp.exe` thì **nên giữ lại và cập nhật thường xuyên** (`yt-dlp.exe -U`): app ưu tiên dùng bản mới hơn giữa nó và bản đóng gói bên trong, nên đây là cách vá lỗi YouTube 403 mà không phải build lại.
