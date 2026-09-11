"""Explicit Jellyfin encoding configuration read/merge/apply. Never changes GPU drivers."""
import json
import threading
from urllib.parse import urlsplit
from urllib.request import Request
from urllib.error import HTTPError
from settings import SettingsError


FIELDS = {
    'NAS_ACCELERATION': 'HardwareAccelerationType',
    'NAS_HARDWARE_ENCODING': 'EnableHardwareEncoding',
    'NAS_VAAPI_DEVICE': 'VaapiDevice', 'NAS_QSV_DEVICE': 'QsvDevice',
    'NAS_DECODING_CODECS': 'HardwareDecodingCodecs', 'NAS_HEVC_10BIT': 'EnableDecodingColorDepth10Hevc',
    'NAS_TONEMAPPING': 'EnableTonemapping', 'NAS_VPP_TONEMAPPING': 'EnableVppTonemapping',
    'NAS_LOW_POWER_H264': 'EnableIntelLowPowerH264HwEncoder', 'NAS_ENCODING_THREADS': 'EncodingThreadCount',
}


class NASConfig:
    def __init__(self, cache):
        self.cache = cache
        self.lock = threading.Lock()

    def validate_origin(self, values):
        origin = values['NAS_ORIGIN']
        if not origin:
            return
        p = urlsplit(origin)
        if (p.scheme != 'https' or p.hostname not in self.cache.hosts or p.port not in (None, 443)
                or p.username or p.password or p.path not in ('', '/') or p.query or p.fragment):
            raise SettingsError('NAS 地址必须是 UPSTREAM_HOSTS 中允许的 HTTPS 主机，不含路径或凭据')

    def request(self, values, body=None):
        self.validate_origin(values)
        if not values['NAS_ORIGIN'] or not values['NAS_API_KEY']:
            raise SettingsError('请先保存 Jellyfin 地址和有管理权限的 API 密钥')
        req = Request(values['NAS_ORIGIN'].rstrip('/') + '/System/Configuration/encoding',
                      data=json.dumps(body).encode() if body is not None else None,
                      headers={'X-Emby-Token': values['NAS_API_KEY'], 'Content-Type': 'application/json',
                               'Accept-Encoding': 'identity'})
        try:
            with self.cache.opener.open(req, timeout=30) as response:
                data = response.read(1024 * 1024 + 1)
                if len(data) > 1024 * 1024 or response.status not in (200, 204):
                    raise SettingsError('Jellyfin 配置响应无效')
                return json.loads(data) if data else None
        except HTTPError as error:
            code = error.code
            error.close()
            raise SettingsError(f'Jellyfin 配置接口 HTTP {code}；请检查管理权限和地址') from None
        except SettingsError:
            raise
        except Exception as error:
            self.cache.debug.exception('nas_config_request_failed', error)
            raise SettingsError('NAS 配置请求失败或超时；若正在应用，请读取当前配置确认是否已生效') from None

    def read(self, values):
        current = self.request(values)
        if not isinstance(current, dict) or any(field not in current for field in FIELDS.values()):
            raise SettingsError('Jellyfin 编码配置字段不兼容；当前按 10.10.7 接口实现')
        return current

    @staticmethod
    def public(current):
        result = {key: current[field] for key, field in FIELDS.items()}
        result['NAS_DECODING_CODECS'] = ','.join(result['NAS_DECODING_CODECS'])
        return result

    def apply(self, values):
        current = self.read(values)
        wanted = {field: values[key] for key, field in FIELDS.items()}
        wanted['HardwareDecodingCodecs'] = [s for s in values['NAS_DECODING_CODECS'].split(',') if s]
        current.update(wanted)  # preserve all fields not managed here
        self.request(values, current)
        actual = self.read(values)
        if any(actual.get(k) != v for k, v in wanted.items()):
            raise SettingsError('已提交 NAS 配置，但回读结果不一致；请在 Jellyfin 控制台核对')
        self.cache.debug.emit('INFO', 'nas_encoding_configuration_applied', force=True, backend=values['NAS_ACCELERATION'])
        return self.public(actual)
