# Registry không còn trả manifest nhưng image vẫn còn local

Lỗi phase 05 `Pull OVN DB images only` xảy ra trước target config/MTU, API freeze
và DB migration. `docker manifest inspect` trả `no such manifest` trong khi
`docker image ls` vẫn có đúng tag là bằng chứng lỗi truy cập manifest hiện tại;
không chứng minh image local hỏng hoặc tag đã bị xóa vĩnh viễn.

## Chính sách image

Phase 05 kiểm tra toàn bộ image cần cho OVN DB, OVN controller và Neutron target
trên đúng host. Nếu đúng reference đã có local, dùng lại. Nếu thiếu, Kolla tải
image đó; lỗi registry vẫn chặn migration trước target config/MTU và downtime.
Không bỏ qua lỗi tải, không tự đổi release/distro/tag hoặc retag image khác.

Helper dùng một bản sao tạm của Ansible tree thuộc Kolla đang cài, thay đúng
action `pull_image` bằng `ensure_image` của Kolla 18.8. Native defaults, service
mapping, inventory overrides, globals.d và registry authentication vẫn do Kolla
xử lý. Installed Kolla không bị chỉnh; chỉ thực thi action pull với policy mới,
không deploy/restart container. Context OVN chỉ áp dụng cho lựa chọn image.

Mỗi lần kiểm tra ghi riêng vào `image-preflight/<invocation-id>/` trong backup:
`result.json`, `kolla-images.log` và `<host>.<role>.json` chứa image ID, tags,
repo digests, architecture, OS. Evidence cũng được đưa vào migration-report.json;
format TXT không đổi. Không lưu image environment hoặc registry password vào
JSON evidence.

Image cache là dữ liệu hữu ích trong trường hợp này. Nếu thiếu `ovn-controller`
trên network1/compute1/compute2 mà registry vẫn lỗi, cần khôi phục đúng image
từ bản lưu hoặc registry nội bộ đã xác minh trước khi tiếp tục. Có ba image DB
trên controller không chứng minh image trên chassis đã đủ.

## Khi preparation lỗi

Giữ nguyên log và evidence của run. Các route resume cũ đã chuyển vào
[archive](../archive/README.md). Với lab cần đo lại toàn bộ migration, sửa nguyên
nhân rồi dùng reset để dựng baseline OVS mới trước khi chạy run mới; không
chạy lại entrypoint migration đầy đủ trên một trạng thái chuẩn bị dở.
