# Ansible plugins

- `callback/migration_task_timing.py`: ghi timing task/host và thu diagnostics
  khi workflow thất bại.
- `filter/run_files.py`: filter `run_file`, dùng chung resolver layout run.

`ansible.cfg` trỏ vào hai thư mục này. Ansible vẫn cần phân thư mục theo loại
plugin dù mỗi loại hiện chỉ có một file.

`playbooks/filter_plugins/project_run_files.py` và
`playbooks/transport/filter_plugins/project_run_files.py` chỉ nạp filter chung,
không chứa bản sao logic. Chúng cần thiết khi một playbook con chạy từ thư mục
khác mà không tìm thấy `ansible.cfg` của project. Test discovery kiểm tra đúng
tình huống này, kể cả chuỗi Python → Ansible → helper được chép sang node.
