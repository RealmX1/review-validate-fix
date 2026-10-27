"""用户本机材料默认路径与显式配置验证。"""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.rvf_user_local_data_paths import resolved_rvf_user_local_data_paths, resolved_rvf_runtime_log_root, resolved_rvf_reviewer_registry_path


def test_default_materials_share_one_home_root(tmp_path):
    paths=resolved_rvf_user_local_data_paths(environment={},home_directory=tmp_path)
    assert paths.user_local_data_root == tmp_path/'.rvf/user-local-data'
    for value in (paths.reviewer_configuration_root,paths.runtime_state_root,paths.run_logs_root,paths.deployment_receipts_root):
        assert paths.user_local_data_root in value.parents


def test_explicit_material_root_controls_all_defaults(tmp_path):
    root=tmp_path/'persistent-materials'
    environment={'RVF_USER_LOCAL_DATA_ROOT':str(root)}
    paths=resolved_rvf_user_local_data_paths(environment=environment)
    assert paths.reviewer_registry_path == root/'reviewer-configuration/reviewer-registry.json'
    assert resolved_rvf_runtime_log_root(environment=environment) == paths.run_logs_root


def test_installed_reviewer_registry_uses_user_materials(tmp_path):
    root=tmp_path/'persistent-materials'
    registry=root/'reviewer-configuration/reviewer-registry.json'
    registry.parent.mkdir(parents=True)
    registry.write_text('{}')
    assert resolved_rvf_reviewer_registry_path(tmp_path/'bundled.json',environment={'RVF_USER_LOCAL_DATA_ROOT':str(root)}) == registry
