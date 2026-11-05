# Student Toolbox

Multi-purpose utility app with Image/PDF tools, media conversion, YouTube downloader, and more.

## Yêu cầu hệ thống

- **Windows 10/11** (64-bit)
- **FFmpeg** (cho Media Converter và YouTube Downloader)

## Cài đặt FFmpeg

### Cách 1: Đặt cạnh exe (khuyến nghị)
1. Tải FFmpeg: https://www.gyan.dev/ffmpeg/builds/
2. Chọn bản **ffmpeg-release-essentials.zip**
3. Giải nén và copy file `ffmpeg.exe` vào cùng thư mục với `ImageToPDF.exe`

### Cách 2: Cài vào PATH
1. Tải và giải nén như trên
2. Thêm thư mục `bin` của FFmpeg vào biến môi trường PATH
3. Khởi động lại terminal/app

## Sử dụng

### Chạy từ source

```bash
pip install -r requirements.txt
python main.py
```

### Chạy từ exe

Chạy `dist\ImageToPDF.exe`

## Tính năng

- **Image → PDF**: Gộp nhiều ảnh thành file PDF
- **Media Converter**: Chuyển đổi video/audio (cần FFmpeg)
  - MP4 ↔ MP3, WAV
  - WAV → MP4
- **YouTube Downloader**: Tải video/audio từ YouTube (cần FFmpeg + License)
- **PDF Tools**: Merge, split, extract, reorder, compress, PDF → images
- **Code Tools**: Number converter, bit viewer, bitmask calculator, ASCII/HEX
- **File Tools**: Batch rename, hash, folder analyzer, cleanup, ZIP

## License

Một số tính năng (YouTube Downloader) cần license key để sử dụng.

Liên hệ để nhận license key.

## Build exe

```bash
pip install pyinstaller
pyinstaller ImageToPDF.spec
```

Exe sẽ nằm trong `dist\ImageToPDF.exe` (~45 MB)

**Lưu ý**: Exe không bundle FFmpeg. User cần cài FFmpeg riêng.

## Cấu trúc project

```
image_to_pdf/
├── main.py                 # Main app
├── license_system.py       # License management
├── license_keygen.py       # Admin tool (tạo license key)
├── requirements.txt        # Python dependencies
├── ImageToPDF.spec         # PyInstaller spec
└── dist/
    └── ImageToPDF.exe      # Executable (sau khi build)
```

## Dependencies

- Python 3.10+
- Pillow
- reportlab
- pypdf
- pypdfium2
- yt-dlp
- cryptography

Xem đầy đủ trong `requirements.txt`

---

**Created by Cao Tín Dương**

Development: Cao Tín Dương × Claude Code × ChatGPT
