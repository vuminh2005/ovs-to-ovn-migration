# Lỗi scope kolla_globals_path trong phase 02

Task `Resolve source globals per host and verify effective tunnel interface`
đã tham chiếu `hostvars['localhost']['kolla_globals_path']`. Biến này được nạp
bằng `vars_files` trong từng play, không phải một host fact của localhost.
Do đó Ansible báo undefined trước khi thực hiện assertion về interface.

Hai tham chiếu trong `playbooks/02-precheck.yml` được thay bằng biến trực tiếp:

```yaml
- (lookup('template', kolla_globals_path) | from_yaml).tunnel_interface == validation_effective_tunnel_interface
- inventory_hostname not in groups['network'] or (lookup('template', kolla_globals_path) | from_yaml).neutron_external_interface == 'ns-ex'
```

Đường dẫn vẫn lấy từ `KOLLA_GLOBALS` hoặc mặc định `/etc/kolla/globals.yml`.
Template vẫn render trong context từng host để kiểm tra `br-underlay` trên
network1, `eth1` trên hai compute và external `ns-ex` trên network1.

Regression test chạy Ansible thực tế trong thư mục/inventory độc lập:

- Chứng minh biến play không tồn tại trong hostvars localhost.
- Thực thi chính assertion của production cho ba chassis; controller được skip.
- Cấu hình external sai vẫn phải thất bại vì assertion, không vì scope/template.

Lần migration bị dừng ở phase 02 chỉ chạy bootstrap/backup/precheck. Workload
đo bắt đầu ở phase 04, staging ở phase 05, thay đổi MTU/config ở phase 06, freeze
ở phase 07. Sau sửa có thể chạy lại entrypoint bình thường để tạo run mới:

```bash
cd /root/ovs-to-ovn-migration
source /root/venvs/kolla-2024.1/bin/activate
source /etc/kolla/admin-openrc.sh
ansible-playbook -i /root/multinode migrate-to-ovn.yml
```

Không dùng `--start-at-task` với entrypoint migration trong tiến trình Ansible
mới: các runtime facts của phase 00 không được phục hồi bằng cách đó. Backup của
run precheck lỗi được giữ để đối chiếu. Không cần reset/deploy lại cho lỗi này.

Nguồn scope Ansible:
https://docs.ansible.com/projects/ansible/latest/playbook_guide/playbooks_vars_facts.html
