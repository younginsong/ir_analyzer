"""
session.py - 분석 세션 저장/불러오기
.irsession 파일 형식 (pickle 기반)
스펙트럼 원본 데이터, 피팅 결과, Potential 할당 등 모든 상태 보존
"""
import pickle
from typing import Any

SESSION_VERSION = 1


_MODULE_ALIASES = {
    "numpy._core": "numpy.core",
    "numpy._core.numeric": "numpy.core.numeric",
    "numpy._core.multiarray": "numpy.core.multiarray",
}


class _CompatUnpickler(pickle.Unpickler):
    """Older sessions may reference legacy NumPy module paths."""

    def find_class(self, module: str, name: str) -> Any:
        module = _MODULE_ALIASES.get(module, module)
        return super().find_class(module, name)


def save_session(filepath: str, data: dict):
    """분석 세션을 .irsession 파일로 저장"""
    payload = {'_version': SESSION_VERSION, **data}
    with open(filepath, 'wb') as f:
        pickle.dump(payload, f, protocol=4)


def load_session(filepath: str) -> dict:
    """
    .irsession 파일 불러오기.
    반환값: save_session 에 전달한 data dict (버전 키 제외)
    """
    with open(filepath, 'rb') as f:
        data = _CompatUnpickler(f).load()
    version = data.pop('_version', 0)
    if version != SESSION_VERSION:
        raise ValueError(
            f"지원하지 않는 세션 파일 버전입니다 (version={version}).\n"
            "최신 IR Analyzer 로 저장한 파일을 사용하세요."
        )
    return data
