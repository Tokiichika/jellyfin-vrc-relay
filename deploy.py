"""Interactive, repeatable Docker Compose deployment. No third-party packages."""
import argparse
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

from settings import encoded, read_env


class DeployError(ValueError):
    pass


def public_origin(value):
    value = value.strip().rstrip('/')
    try:
        part = urlsplit(value)
        if (part.scheme != 'https' or not part.hostname or part.username or part.password
                or part.path or part.query or part.fragment or any(c.isspace() for c in value)):
            raise ValueError()
        if part.port is not None and not 1 <= part.port <= 65535:
            raise ValueError()
    except ValueError:
        raise DeployError('公开地址必须是 HTTPS 根地址，例如 https://relay.example.com，不含路径或密钥') from None
    return value


def upstream_hosts(value):
    result = []
    for raw in value.split(','):
        host = raw.strip().lower()
        if not host:
            continue
        try:
            host = host.encode('idna').decode('ascii')
        except UnicodeError:
            raise DeployError('Jellyfin 主机名格式不正确') from None
        if len(host) > 253 or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in host.split('.')):
            raise DeployError('Jellyfin 主机只填写域名或 IPv4 地址，多个用逗号分隔，不带协议、端口或路径')
        if host not in result:
            result.append(host)
    return ','.join(result)


def validate_existing(text):
    values = read_env(text)
    key = values.get('ADMIN_TOKEN', '')
    if len(key) < 32 or key.startswith('replace-'):
        raise DeployError('已有配置的 ADMIN_TOKEN 无效；为避免意外更换密钥，请先手动修正该文件')
    public_origin(values.get('PUBLIC_BASE_URL', ''))
    upstream_hosts(values.get('UPSTREAM_HOSTS', ''))
    return values


def prepare(root, url=None, hosts=None, interactive=True, ask=input):
    """Create once, or reuse/migrate existing configuration without changing its values."""
    directory = root / 'config'
    target = directory / '.env'
    if directory.is_symlink() or target.is_symlink():
        raise DeployError('config 目录及 config/.env 不能是符号链接')
    if target.exists():
        values = validate_existing(target.read_text(encoding='utf-8'))
        return target, values, False
    legacy = root / '.env'
    if legacy.is_symlink():
        raise DeployError('旧 .env 不能是符号链接')
    if legacy.exists():
        text = legacy.read_text(encoding='utf-8')
        values = validate_existing(text)
        created_key = False
    else:
        if url is None:
            if not interactive:
                raise DeployError('首次非交互安装需要 --public-url')
            url = ask('公开 HTTPS 地址（例如 https://relay.example.com）：')
        url = public_origin(url)
        if hosts is None:
            if not interactive:
                raise DeployError('首次非交互安装需要 --jellyfin-host；仅使用 B 站时传入空字符串')
            hosts = ask('Jellyfin 主机名（例如 jellyfin.example.com；仅 B 站可留空）：')
        hosts = upstream_hosts(hosts)
        text = (root / '.env.example').read_text(encoding='utf-8')
        updates = {'PUBLIC_BASE_URL': url, 'UPSTREAM_HOSTS': hosts, 'ADMIN_TOKEN': secrets.token_urlsafe(48)}
        for key, value in updates.items():
            text, count = re.subn(r'^' + key + r'=.*$', lambda _: key + '=' + encoded(value), text, flags=re.M)
            if count != 1:
                raise DeployError('.env.example 配置模板不完整')
        values = validate_existing(text)
        created_key = True
    directory.mkdir(mode=0o700, exist_ok=True)
    # Link a completed temporary file into place; fail rather than overwrite a racing installer.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix='.init-', delete=False, mode='w', encoding='utf-8', newline='\n') as file:
            temporary = Path(file.name)
            os.chmod(temporary, 0o600)
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.link(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target, values, created_key


def set_permissions(path):
    os.chmod(path.parent, 0o700)
    os.chmod(path, 0o600)
    os.chown(path.parent, 10001, 10001)
    os.chown(path, 10001, 10001)


def command(args, root, capture=False):
    from live_setup import compose_args
    args = compose_args(args, root)
    try:
        return subprocess.run(args, cwd=root, check=True, text=True, capture_output=capture)
    except FileNotFoundError:
        raise DeployError('未找到 Docker，请先通过系统或 1Panel 安装 Docker 和 Compose 插件') from None
    except subprocess.CalledProcessError:
        raise DeployError('Docker 命令失败，请检查 Docker 服务、Compose 插件、镜像访问及上方错误；配置和缓存已保留') from None


def wait_healthy(root, timeout=90):
    container = command(['docker', 'compose', 'ps', '-q', 'relay'], root, True).stdout.strip()
    if not container or '\n' in container:
        raise DeployError('无法确认 relay 容器；请运行 bash compose.sh ps 检查')
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = command(['docker', 'inspect', '--format', '{{.State.Health.Status}}', container], root, True).stdout.strip()
        if status == 'healthy':
            return
        if status == 'unhealthy':
            break
        time.sleep(2)
    raise DeployError('容器未通过健康检查，请运行 bash compose.sh logs --tail=100 relay mediamtx；配置和缓存已保留')


def main():
    parser = argparse.ArgumentParser(description='一起看 Relay 一键部署：自动生成配置、设置权限并启动 Docker')
    parser.add_argument('--public-url', help='首次安装的公开 HTTPS 地址')
    parser.add_argument('--jellyfin-host', help='首次安装的 Jellyfin 域名，多个用逗号分隔；可为空')
    parser.add_argument('--non-interactive', action='store_true', help='禁止交互提问')
    parser.add_argument('--init-only', action='store_true', help='仅生成/迁移配置及设置权限，不启动 Docker')
    args = parser.parse_args()
    if not sys.platform.startswith('linux') or os.geteuid() != 0:
        raise DeployError('部署脚本需在 Linux 上以 root 运行：sudo bash install.sh')
    root = Path(__file__).resolve().parent
    os.umask(0o077)
    if not args.init_only:
        command(['docker', 'version'], root, True)
        command(['docker', 'compose', 'version'], root, True)
    path, values, created_key = prepare(root, args.public_url, args.jellyfin_host, not args.non_interactive)
    from live_setup import prepare_live
    try:
        prepare_live(root)
    except ValueError as error:
        raise DeployError(str(error)) from None
    set_permissions(path)
    set_permissions(root / 'config' / 'mediamtx.yml')
    print('配置已准备：config/.env（重复运行保留已有配置与密钥）', flush=True)
    if created_key:
        print('首次生成的管理密钥（请保存，不要发布）：' + values['ADMIN_TOKEN'], flush=True)
    if args.init_only:
        print('初始化完成；可执行 bash compose.sh up -d --build 启动。')
        return
    command(['docker', 'compose', 'up', '-d', '--build'], root)
    wait_healthy(root)
    live_check = '''import json, os, time, urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
for attempt in range(15):
    req = urllib.request.Request('http://127.0.0.1:8080/api/live', headers={'Authorization': 'Bearer ' + os.environ['ADMIN_TOKEN']})
    with opener.open(req, timeout=3) as response:
        state = json.load(response)
    if state['enabled'] and state['gateway'] and state['online']:
        break
    time.sleep(1)
else:
    raise SystemExit('Live engine unavailable; inspect relay and mediamtx logs')
'''
    command(['docker', 'compose', 'exec', '-T', 'relay', 'python', '-c', live_check], root, True)
    print('容器启动成功，健康检查通过。')
    print('管理入口：' + values['PUBLIC_BASE_URL'])
    print('请按 README 配置 HTTPS 反向代理；18080 仅绑定本机，不会自动配置 DNS、证书或防火墙。')
    print('直播引擎已连接。请放行 config/.env 中 LIVE_RTMP_PORT / LIVE_RTSP_PORT 对应的 TCP 端口（默认 1935 / 8554）。')
    if not created_key:
        print('继续使用原管理密钥；如需查看：sudo grep "^ADMIN_TOKEN=" config/.env')


if __name__ == '__main__':
    try:
        main()
    except (DeployError, OSError, EOFError, KeyboardInterrupt) as error:
        print('部署未完成：' + (str(error) or '操作已中止'), file=sys.stderr)
        raise SystemExit(1)
