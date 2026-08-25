# EVN CSKH Monitor

**EVN CSKH Monitor** là một custom integration Home Assistant độc lập, có domain mới:

```text
evn_cskh_monitor
```

Integration có domain, component, API WebUI và thư mục dữ liệu riêng. Dữ liệu được lưu tại:

```text
/config/evncskh/evncskh.db
```

> `NPC` vẫn xuất hiện trong lựa chọn khu vực và mã nguồn API vì đó là tên kỹ thuật chính thức của **EVN Northern Power Corporation**, không phải tên/domain của integration.

## Điểm chính

- Hỗ trợ các backend EVN: HN, NPC, CPC, SPC, HCMC theo logic API của source dự án.
- Login và kiểm tra mã khách hàng ngay trong Config Flow.
- Lưu dữ liệu chuẩn hóa vào SQLite và đồng thời lưu nguyên response server khác nhau vào `raw_server_records` để không làm mất field chưa được parser sử dụng.
- Tiền hóa đơn chính thức từ EVN luôn được ưu tiên. Integration không gán một đơn giá cố định cho từng ngày.
- Sensor chỉ đọc snapshot trong RAM; không mở SQLite và không gọi mạng khi Home Assistant render state.
- Mọi SQLite/file I/O chạy trong executor.
- Lần khởi động chỉ mở DB/cache cục bộ. EVN refresh và backfill lịch sử chạy bằng config-entry background task, không giữ Home Assistant ở pha startup.
- Dữ liệu gần nhất được ưu tiên trước; lịch sử cũ từ năm 2020 được backfill chậm ở nền, theo batch và có pause giữa các request.
- WebUI riêng tại **EVN CSKH Monitor** trong sidebar, không dùng CDN ngoài.
- Có diagnostics, reauthentication và unload/reload theo lifecycle Home Assistant hiện tại.

## Cài đặt

### Cài tự động

  - Nhấn nút bên dưới để thêm vào HACS trên Home Assistant.

  [![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=khaisilk1910&repository=EVN-CSKH-Monitor&category=integration)

  - Sau khi thêm trong HACS và khởi động lại Home Assistant
     
  - Vào Settings -> Integrations -> Add integration -> Tìm `EVN CSKH Monitor`
    
### HACS

Dùng repository này như một **Custom repository / Integration**, cài `EVN CSKH Monitor`, sau đó restart Home Assistant.

### Cài thủ công

Chép thư mục:

```text
custom_components/evn_cskh_monitor
```

vào:

```text
/config/custom_components/evn_cskh_monitor
```

sau đó restart Home Assistant.

Vào **Settings → Devices & services → Add integration → EVN CSKH Monitor** và nhập:

- Khu vực EVN
- Username
- Password
- Mã khách hàng/công tơ

Mỗi mã khách hàng là một config entry riêng và có unique ID riêng.

## Cấu hình Zalo Bot

Mở **Configure** của config entry EVN CSKH Monitor. Các tùy chọn:

- `zalo_type`: `0` = User, `1` = Group
- `zalo_account_selection`
- `zalo_thread_id`
- Bật/tắt gửi hóa đơn PNG/PDF
- Bật/tắt gửi sản lượng hằng ngày
- Bật/tắt gửi lịch cắt điện

Integration gọi đúng các action của `zalo_bot` nếu service tồn tại.

### PNG hóa đơn

```yaml
action: zalo_bot.send_image
data:
  type: 0
  image_path: /config/evncskh/PD05000140586_7_2026.png
  message: Hóa đơn tháng 7/2026 của công tơ PD05000140586
  thread_id: "432432432342"
  account_selection: "+84435452324"
```

### PDF hóa đơn

```yaml
action: zalo_bot.send_file
data:
  type: 0
  account_selection: "+84324343243"
  thread_id: "4343243243223"
  message: Chi tiết tiền điện tháng 7/2026 công tơ PD05000140586
  file_path_or_url: /config/evncskh/PD05000140586_7_2026.pdf
```

Integration chỉ gửi file thật đã tồn tại hoặc file PDF/PNG hợp lệ mà response EVN trực tiếp cung cấp URL/base64 để lưu. Không tự dựng hóa đơn giả.

Thông báo hằng ngày được chống gửi trùng theo ngày + giá trị `tieu_thu_hom_qua`; thông báo cắt điện và file hóa đơn cũng có fingerprint riêng.

## WebUI và tính chính xác

WebUI dùng API có xác thực Home Assistant:

```text
/api/evncskh/options
/api/evncskh/monthly/{account}
/api/evncskh/daily/{account}
/api/evncskh/summary/{account}
```

Dashboard hiển thị:

- Tổng kWh thực có trong DB
- Tổng tiền hóa đơn EVN chính thức
- Trung bình kWh/ngày
- Độ phủ dữ liệu và số ngày thiếu
- Đỉnh/thấp nhất theo ngày
- Số hóa đơn chính thức
- Số raw response server đã lưu
- Tiền nợ
- Danh sách file PNG/PDF hóa đơn
- Lịch cắt điện tương lai
- Lỗi từng phần của lần đồng bộ gần nhất
- Bảng chi tiết ngày/tháng

Tiền điện theo từng ngày **không được bịa bằng `kWh × đơn giá cố định`**. Biểu giá bậc thang chỉ được dùng cho ước tính kỳ khi không có hóa đơn chính thức; trường hợp có hóa đơn EVN thì số tiền EVN được dùng làm nguồn chuẩn.

## Thư mục dữ liệu

EVN CSKH Monitor sử dụng thư mục dữ liệu riêng `/config/evncskh` và database `/config/evncskh/evncskh.db`.

## Gỡ bỏ

1. Xóa config entry **EVN CSKH Monitor** trong Home Assistant.
2. Gỡ integration qua HACS hoặc xóa `/config/custom_components/evn_cskh_monitor`.
3. Nếu không cần lịch sử nữa, tự xóa `/config/evncskh`.

Dữ liệu trong `/config/evncskh` không bị tự động xóa khi gỡ integration để tránh mất lịch sử ngoài ý muốn.
