"""Apply 2.8.0 to a compatible 2.5.1/2.6.x/2.7.x/2.8.x deployment."""
import ast
import argparse
from datetime import datetime
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys

FILES = (
    'app.py', 'hls.py', 'metrics.py', 'diagnostics.py', 'throttle.py',
    'preload.py', 'bili.py', 'mp4.py', 'system_stats.py', 'settings.py',
    'nas_config.py', 'index.html', 'ui.css', 'ui.js', 'settings-app.js',
    'Dockerfile', 'deploy.py', 'install.sh', '.env.example',
    'README.md', 'CHANGELOG.md', 'UPDATE.md', 'RELEASE_CHECKLIST.md',
)


def deployment_version(target):
    """Read literal version fields without importing/executing installed code."""
    try:
        tree = ast.parse((target / 'app.py').read_text(encoding='utf-8-sig'))
    except (SyntaxError, UnicodeError) as error:
        raise ValueError('无法解析原目录 app.py，请核对部署目录和文件完整性。') from error
    versions = set()
    def collect(value):
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            if re.fullmatch(r'\d+\.\d+\.\d+', value.value):
                versions.add(value.value)
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == 'version':
                    collect(value)
        elif isinstance(node, ast.keyword) and node.arg == 'version':
            collect(node.value)
        elif isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id in ('version', 'VERSION', '__version__') for t in node.targets):
                collect(node.value)
    if len(versions) != 1:
        raise ValueError('app.py 版本标识缺失或不一致：' + (', '.join(sorted(versions)) or '未识别') + '；请核对原部署目录。')
    version = versions.pop()
    if version != '2.5.1' and not re.fullmatch(r'2\.(6|7|8)\.\d+', version):
        raise ValueError(f'检测到 app.py 版本 {version}；当前支持 2.5.1、2.6.x、2.7.x/2.8.x。')
    return version


def apply(source, target):
    source, target = Path(source).resolve(), Path(target).resolve()
    if source == target:
        raise ValueError('请把更新包解压到独立目录，再指定原部署目录；不要先覆盖原文件。')
    if not (target / 'compose.yaml').is_file() or not (target / 'app.py').is_file():
        raise ValueError('目标不是原部署目录：缺少 compose.yaml 或 app.py。')
    config = target / 'config' / '.env'
    if not config.is_file():
        raise ValueError('未找到 config/.env；请指定使用 config/.env 的原部署目录。')
    deployment_version(target)
    required = ('settings.py', 'hls.py', 'nas_config.py', 'index.html', 'ui.js', 'settings-app.js')
    missing = [name for name in required if not (target / name).is_file()]
    if missing:
        raise ValueError('原部署结构不完整，缺少：' + ', '.join(missing))
    for name in FILES:
        if not (source / name).is_file() or (source / name).is_symlink():
            raise ValueError('更新包文件缺失或无效：' + name)
        if (target / name).is_symlink() or ((target / name).exists() and not (target / name).is_file()):
            raise ValueError('拒绝覆盖非普通文件：' + name)
    backups = target / '.upgrade-backups'
    if backups.is_symlink():
        raise ValueError('备份目录不能是符号链接。')
    backup = backups / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    backup.mkdir(parents=True, mode=0o700)
    existing = [name for name in FILES if (target / name).exists()]
    for name in existing:
        shutil.copy2(target / name, backup / name)
    # Finish the entire backup before replacing any application files.
    try:
        for name in FILES:
            shutil.copyfile(source / name, target / name)
            os.chmod(target / name, 0o755 if name.endswith('.sh') else 0o644)
    except Exception:
        for name in existing:
            shutil.copy2(backup / name, target / name)
        raise
    return backup


def main():
    parser = argparse.ArgumentParser(description='升级到 2.8.0，保留原配置、Compose 项目和缓存卷')
    parser.add_argument('target', help='原部署目录的绝对路径，例如 /opt/jellyfin-vrc-relay')
    args = parser.parse_args()
    if not sys.platform.startswith('linux') or os.geteuid() != 0:
        raise ValueError('请在 Linux 上运行：sudo bash update.sh /原部署目录')
    target = Path(args.target)
    if not target.is_absolute():
        raise ValueError('请提供原部署目录的绝对路径。')
    target = target.resolve()
    subprocess.run(['docker', 'compose', 'version'], check=True)
    print('原部署目录：' + str(target), flush=True)
    print('检测到源码版本：' + deployment_version(target), flush=True)
    backup = apply(Path(__file__).resolve().parent, target)
    print('旧程序备份：' + str(backup), flush=True)
    print('保留 compose.yaml、反向代理配置、管理密钥和缓存卷；正在重建原服务。', flush=True)
    # Invoke from the original directory to preserve the Compose project/volume name.
    subprocess.run([sys.executable, str(target / 'deploy.py'), '--non-interactive'], cwd=target, check=True)
    print('2.8.0 更新完成。浏览器刷新后即可使用最近创建 / 最近复制记录。')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print('升级未完成：' + str(error), file=sys.stderr)
        print('配置和缓存未删除。若重建或健康检查失败，请查看日志及 UPDATE.md 的回退步骤。', file=sys.stderr)
        raise SystemExit(1)
