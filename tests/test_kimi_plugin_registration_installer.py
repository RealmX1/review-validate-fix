"""Kimi 插件注册安装器（S-B8）的行为闸。

Kimi 的插件注册在四条跨进程通道（CLI 子命令 / REST / WebSocket / ACP）上**全部不可达**，
所以只能由安装器写 ``plugins/installed.json``。这带来两条本测试必须钉住的风险：

1. **全量重写会掀掉用户装的其它插件** ⇒ 必须是幂等 additive merge。
2. **``state`` 写不进去**（``persist()`` 只落 9 个字段）⇒ 「安装器写完了」永远不等于
   「注册生效了」。最后一条测试把安装器与独立复算的核对器串起来，就是为了让这个闭环
   有真凭据，而不是靠回执自证。

⚠️ 全程用隔离的 ``KIMI_CODE_HOME``，绝不碰真实 ``~/.kimi-code``（live infra）。
"""

from __future__ import annotations

import hashlib
import inspect
import json
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import install_to_codex  # noqa: E402

ACTIVATION_STATE_CHECKER = (
    REPOSITORY_ROOT / "scripts/kimi_plugin_manifest_activation_state_check.py"
)


SHIPPED_KIMI_PLUGIN_MANIFEST = (
    REPOSITORY_ROOT / "plugins/review-validate-fix/.kimi-plugin/plugin.json"
)


def _build_marketplace_root(root: Path) -> Path:
    """造一个最小的 active marketplace root，**复用仓库里真实发布的那份 Kimi manifest**。

    ⚠️ 这里刻意**不**手写一份 manifest。手写副本正是 S-B8 那个缺陷能活下来的原因：真实
    manifest 漏声明 ``skills``（⇒ Kimi 侧 6 个 skill 全部不存在，且零报错），而 fixture 里那份
    手抄件同样漏了，于是闭环测试对着一份「与发布物不同的东西」判了绿。读真文件之后，
    发布物一旦少声明什么，下面那条 independently-verifiable 测试就会当场红。
    """
    plugin_root = root / "plugins" / "review-validate-fix"
    (plugin_root / ".kimi-plugin").mkdir(parents=True)
    manifest_text = SHIPPED_KIMI_PLUGIN_MANIFEST.read_text(encoding="utf-8")
    (plugin_root / ".kimi-plugin" / "plugin.json").write_text(manifest_text, encoding="utf-8")

    # manifest 声明的目录必须在 plugin root 内真实存在，否则 Kimi 判 severity=error/warn。
    # 只复制目录骨架与 SKILL.md，避免把整棵 skills/ 树拷进 tmp。
    manifest = json.loads(manifest_text)
    for declared in manifest.get("skills", []):
        source_directory = SHIPPED_KIMI_PLUGIN_MANIFEST.parent.parent / declared
        for shipped_skill in sorted(source_directory.glob("*/SKILL.md")):
            mirrored = plugin_root / declared / shipped_skill.parent.name / "SKILL.md"
            mirrored.parent.mkdir(parents=True, exist_ok=True)
            mirrored.write_text(shipped_skill.read_text(encoding="utf-8"), encoding="utf-8")
    for declared in manifest.get("commands", []):
        (plugin_root / declared).mkdir(parents=True, exist_ok=True)
    return root


def _read_registry(kimi_home: Path) -> dict:
    return json.loads((kimi_home / "plugins" / "installed.json").read_text(encoding="utf-8"))


def test_first_registration_writes_exactly_the_shape_kimi_itself_persists(
    tmp_path: Path,
) -> None:
    marketplace_root = _build_marketplace_root(tmp_path / "marketplace")
    kimi_home = tmp_path / "kimi-home"

    receipt = install_to_codex.configure_kimi_plugin_registration(
        marketplace_root, kimi_code_home=kimi_home, now="2026-08-24T00:00:00Z"
    )

    assert receipt["was_already_registered"] is False, receipt
    # 两条命令缺一不可：前者刷进程级注册表，后者重建会话（skill 只在会话构造时加载）。
    assert receipt["requires_manual_activation_commands_in_order"] == [
        "/plugins reload",
        "/reload",
    ], receipt
    records = _read_registry(kimi_home)["plugins"]
    assert len(records) == 1, records
    # 全新注册应当与 Kimi 自己 install 本地插件时写下的形状**逐字一致**：`capabilities` 与
    # `github` 在它那边都是 undefined ⇒ 键根本不落盘。写 `null` 会被 0.38.0 的
    # listPlugins 出站 schema 拒收，`/plugins` 面板整个打不开。
    assert set(records[0]) == {
        "id", "root", "source", "enabled", "installedAt",
        "updatedAt", "originalSource",
    }, "字段集合必须与 Kimi 的 persist() 对本地路径安装写出的形状一致"
    assert records[0]["source"] == "local-path", (
        "source 是**来源种类枚举**不是路径；写路径会让 /plugins 报 output validation failed"
    )
    assert records[0]["originalSource"].endswith("plugins/review-validate-fix"), records
    assert records[0]["enabled"] is True, records
    assert "state" not in records[0], "state 是内存派生值，写进去是自欺"


def test_first_registration_writes_the_document_envelope_kimi_itself_persists(
    tmp_path: Path,
) -> None:
    """上一条测试只盖 **record**；这条盖**承载它的 document**。

    Kimi 的 ``persist()`` 落的信封逐字是 ``{version: 1, plugins: [...]}``，它的空 registry
    常量同样是 ``{version: 1, plugins: []}``。当前 ``readInstalled()`` 只校验
    ``Array.isArray(parsed.plugins)`` ⇒ 缺 ``version`` 今天不报错——但这正是
    `source`/`github` 那两条真缺陷的同款形状：**旧版容忍、新版加校验就变硬失败**。
    所以信封也要与宿主逐字一致，而不是只对齐 record。
    """
    marketplace_root = _build_marketplace_root(tmp_path / "marketplace")
    kimi_home = tmp_path / "kimi-home"

    install_to_codex.configure_kimi_plugin_registration(
        marketplace_root, kimi_code_home=kimi_home, now="2026-08-24T00:00:00Z"
    )

    document = _read_registry(kimi_home)
    assert set(document) == {"version", "plugins"}, (
        f"全新 registry 的顶层键集必须与 Kimi 的 writeInstalled() 一致：{document}"
    )
    assert document["version"] == 1, document


def test_existing_document_version_is_preserved_instead_of_being_forced_back_down(
    tmp_path: Path,
) -> None:
    """已存在的 document 保留它自己的 ``version``，不被我们降级。

    补 ``version`` 是为了对齐宿主，不是为了夺走这个字段的所有权：如果用户或未来版本的 Kimi
    已经把 registry 升到更高的 schema 版本，我们这条 additive merge 把它写回 ``1``，就等于
    用一个「兼容性修复」制造出一个真正的降级故障。
    """
    marketplace_root = _build_marketplace_root(tmp_path / "marketplace")
    kimi_home = tmp_path / "kimi-home"
    (kimi_home / "plugins").mkdir(parents=True)
    (kimi_home / "plugins" / "installed.json").write_text(
        json.dumps({"version": 2, "plugins": []}), encoding="utf-8"
    )

    install_to_codex.configure_kimi_plugin_registration(
        marketplace_root, kimi_code_home=kimi_home, now="2026-08-24T00:00:00Z"
    )

    document = _read_registry(kimi_home)
    assert document["version"] == 2, (
        f"已存在的 document version 被覆写了，这是降级不是兼容：{document}"
    )


def test_reinstall_is_idempotent_and_preserves_other_plugins(tmp_path: Path) -> None:
    """全量重写会掀掉用户装的其它插件——这是本函数最容易写错、且后果最严重的地方。"""
    marketplace_root = _build_marketplace_root(tmp_path / "marketplace")
    kimi_home = tmp_path / "kimi-home"
    (kimi_home / "plugins").mkdir(parents=True)
    (kimi_home / "plugins" / "installed.json").write_text(
        json.dumps({"plugins": [{"id": "someone-elses-plugin", "root": "/x", "enabled": True}]}),
        encoding="utf-8",
    )

    first = install_to_codex.configure_kimi_plugin_registration(
        marketplace_root, kimi_code_home=kimi_home, now="2026-08-24T00:00:00Z"
    )
    second = install_to_codex.configure_kimi_plugin_registration(
        marketplace_root, kimi_code_home=kimi_home, now="2026-08-25T00:00:00Z"
    )

    assert first["was_already_registered"] is False, first
    assert second["was_already_registered"] is True, second
    records = _read_registry(kimi_home)["plugins"]
    assert [r["id"] for r in records].count("review-validate-fix") == 1, records
    assert any(r["id"] == "someone-elses-plugin" for r in records), (
        f"别人的插件被掀掉了：{records}"
    )
    ours = next(r for r in records if r["id"] == "review-validate-fix")
    assert ours["installedAt"] == "2026-08-24T00:00:00Z", "重复部署不该刷掉首次安装时间"
    assert ours["updatedAt"] == "2026-08-25T00:00:00Z", ours


def test_malformed_existing_registry_fails_closed_instead_of_overwriting(tmp_path: Path) -> None:
    """Kimi 对非法 JSON 是整体加载失败，不是当空 registry ⇒ 覆盖会掩盖用户那边的问题。"""
    marketplace_root = _build_marketplace_root(tmp_path / "marketplace")
    kimi_home = tmp_path / "kimi-home"
    (kimi_home / "plugins").mkdir(parents=True)
    registry_path = kimi_home / "plugins" / "installed.json"
    registry_path.write_text("{not json", encoding="utf-8")

    try:
        install_to_codex.configure_kimi_plugin_registration(
            marketplace_root, kimi_code_home=kimi_home
        )
    except install_to_codex.OfficialCliInstallationError as error:
        assert "不是合法 JSON" in str(error), error
    else:
        raise AssertionError("非法 registry 必须 fail closed")

    assert registry_path.read_text(encoding="utf-8") == "{not json", "失败路径不得改动原文件"


def test_installer_output_is_independently_verifiable_as_actually_active(tmp_path: Path) -> None:
    """闭环：安装器写完后，**独立复算**的核对器必须判为 sole_registration。

    这条测试存在的全部理由是拒绝「回执写了 = 事情成了」。核对器不读安装器的回执，
    也不读 installed.json 里并不存在的 state，而是自己解析 manifest、逐条复刻 hook schema。
    """
    marketplace_root = _build_marketplace_root(tmp_path / "marketplace")
    kimi_home = tmp_path / "kimi-home"

    install_to_codex.configure_kimi_plugin_registration(
        marketplace_root, kimi_code_home=kimi_home
    )

    completed = subprocess.run(
        [sys.executable, str(ACTIVATION_STATE_CHECKER), "--json", "--kimi-code-home", str(kimi_home)],
        capture_output=True,
        text=True,
    )
    report = json.loads(completed.stdout)
    assert report["activation_state"] == "sole_registration", report
    assert completed.returncode == 0, report
    assert sorted(report["live_hook_event_names"]) == ["Stop", "UserPromptSubmit"], report
    # hook 活着还不够：Kimi 只从 manifest.skills 发现 skill，漏声明就是「插件健康、入口全无」
    # 的零报错状态。这条断言是那个形状的闸。
    assert "review-validate-fix" in report["discovered_skill_directory_names"], report


def test_kimi_home_override_is_honoured(tmp_path: Path) -> None:
    """生产 override 必须生效，否则测试与部署都会打到真实 ~/.kimi-code。"""
    resolved = install_to_codex.resolve_kimi_code_home(
        environment={"KIMI_CODE_HOME": str(tmp_path / "custom")}
    )
    assert resolved == tmp_path / "custom"
    fallback = install_to_codex.resolve_kimi_code_home(
        environment={}, home_directory=tmp_path / "fake-home"
    )
    assert fallback == tmp_path / "fake-home" / ".kimi-code"


def test_receipt_carries_post_write_verification_that_matches_what_is_on_disk(
    tmp_path: Path,
) -> None:
    """回执必须带**可对账**的写后复验证据，而不是只声称写过。

    这条写入无锁无 CAS，运行中的 Kimi 会用内存里的旧 registry 全量重写它。``registry_sha256``
    是给 ``kimi_plugin_manifest_activation_state_check.py`` 事后重算对比用的锚——对不上就说明
    注册在部署之后被覆盖了。
    """
    marketplace_root = _build_marketplace_root(tmp_path / "marketplace")
    kimi_home = tmp_path / "kimi-home"
    (kimi_home / "plugins").mkdir(parents=True)
    (kimi_home / "plugins" / "installed.json").write_text(
        json.dumps({"plugins": [{"id": "someone-elses-plugin", "root": "/x", "enabled": True}]}),
        encoding="utf-8",
    )

    receipt = install_to_codex.configure_kimi_plugin_registration(
        marketplace_root, kimi_code_home=kimi_home
    )

    verification = receipt["post_write_verification"]
    on_disk = (kimi_home / "plugins" / "installed.json").read_bytes()
    assert verification["registry_sha256"] == hashlib.sha256(on_disk).hexdigest(), (
        f"复验记录的 sha256 与磁盘内容对不上，事后对账就是假的：{verification}"
    )
    assert verification["plugin_ids"] == ["review-validate-fix", "someone-elses-plugin"], (
        verification
    )


def test_post_write_verification_fails_closed_when_a_concurrent_kimi_overwrites_us(
    tmp_path: Path, monkeypatch
) -> None:
    """模拟丢更新竞争：``replace`` 之后被运行中的 Kimi 用旧 registry 全量重写 ⇒ 必须当场炸。

    没有这条，写后复验就可能退化成「读回来但从不比对」的装饰。
    """
    marketplace_root = _build_marketplace_root(tmp_path / "marketplace")
    kimi_home = tmp_path / "kimi-home"
    original_replace = Path.replace

    def replace_then_simulate_concurrent_kimi_full_rewrite(self: Path, target):
        original_replace(self, target)
        Path(target).write_text(
            json.dumps({"plugins": [{"id": "someone-elses-plugin"}]}),
            encoding="utf-8",
        )

    monkeypatch.setattr(Path, "replace", replace_then_simulate_concurrent_kimi_full_rewrite)

    try:
        install_to_codex.configure_kimi_plugin_registration(
            marketplace_root, kimi_code_home=kimi_home
        )
    except install_to_codex.OfficialCliInstallationError as error:
        assert "写后复验失败" in str(error), error
    else:
        raise AssertionError("被覆盖掉的注册必须 fail closed，而不是回报成功")


def test_kimi_registration_failure_is_degraded_to_a_receipt_field_not_a_deploy_failure(
    tmp_path: Path,
) -> None:
    """Kimi 是部署链最后一环：它失败**不得**让部署回执写不出来。

    调用它时 Codex/Claude 的部署已不可回滚地完成，只差写回执；而回执是本仓判 deploy-lag 的
    唯一凭据。让第三方产品的一个坏文件把回执留在旧版，比第三个 host 少一个 hook 危险得多。
    """
    def register_that_raises(active_marketplace_root: Path) -> dict:
        raise install_to_codex.OfficialCliInstallationError("installed.json 不是合法 JSON")

    registration, failure = (
        install_to_codex.kimi_plugin_registration_outcome_for_deployment_receipt(
            tmp_path / "marketplace",
            enabled=True,
            register_kimi_plugin=register_that_raises,
        )
    )

    assert registration is None, registration
    assert failure is not None and "不是合法 JSON" in failure["message"], failure
    assert failure["error_type"] == "OfficialCliInstallationError", failure


def test_kimi_registration_outcome_distinguishes_disabled_from_failed(
    tmp_path: Path,
) -> None:
    """「没启用」与「失败了」必须能在回执里分开——两件事压成一个空字段就读不出真相。"""
    def register_that_must_not_run(active_marketplace_root: Path) -> dict:
        raise AssertionError("未启用时不得调用注册")

    assert install_to_codex.kimi_plugin_registration_outcome_for_deployment_receipt(
        tmp_path / "marketplace",
        enabled=False,
        register_kimi_plugin=register_that_must_not_run,
    ) == (None, None)

    marketplace_root = _build_marketplace_root(tmp_path / "marketplace-real")
    registration, failure = (
        install_to_codex.kimi_plugin_registration_outcome_for_deployment_receipt(
            marketplace_root,
            enabled=True,
            register_kimi_plugin=lambda root: install_to_codex.configure_kimi_plugin_registration(
                root, kimi_code_home=tmp_path / "kimi-home"
            ),
        )
    )
    assert failure is None, failure
    assert registration is not None and registration["was_already_registered"] is False


def test_main_routes_kimi_registration_through_the_non_fatal_outcome_helper() -> None:
    """结构性守卫：main() 不得再直接调用会抛的注册函数、也不得漏记失败字段。

    RVF-C 的成因就是 main() 里那一句裸调用；换回去时这条必须当场红，否则修复会被悄悄回退。
    """
    main_source = inspect.getsource(install_to_codex.main)
    assert "kimi_plugin_registration_outcome_for_deployment_receipt(" in main_source
    assert "configure_kimi_plugin_registration(" not in main_source, (
        "main() 又在直接调用会抛的注册函数了 ⇒ 它一失败，部署回执就写不出来"
    )
    assert '"kimi_plugin_registration_failure": kimi_plugin_registration_failure' in main_source


def test_shipped_kimi_manifest_declares_the_skill_directory(tmp_path: Path) -> None:
    """发布物必须自己声明 skills——Kimi 不会去扫 ``skills/``。

    ``countDiscoveredPluginSkills()`` 逐字是 ``(manifest?.skills ?? []).map(...)``，
    ``roots.length === 0`` 时直接 ``return 0``。对照：``agents`` 在字段缺省时会自动收
    ``agents/`` 目录，``skills`` **没有**这个待遇；唯一 fallback 是 plugin 根上的 ``SKILL.md``，
    RVF 没有。⇒ 漏声明 = 6 个 skill 全部不存在，而 plugin ``state`` 仍是 ``ok``。
    """
    manifest = json.loads(SHIPPED_KIMI_PLUGIN_MANIFEST.read_text(encoding="utf-8"))
    declared = manifest.get("skills")
    assert declared, "Kimi manifest 必须声明 skills，否则 RVF 在 Kimi 上没有任何入口"
    assert all(entry.startswith("./") for entry in declared), (
        'skills 条目必须以 "./" 开头，否则 Kimi 判 severity=error ⇒ 连 hook 一起停摆'
    )
    payload_root = SHIPPED_KIMI_PLUGIN_MANIFEST.parent.parent
    for entry in declared:
        assert (payload_root / entry).is_dir(), f"{entry} 在发布物里不是目录"
        assert list((payload_root / entry).glob("*/SKILL.md")), f"{entry} 下没有任何 SKILL.md"


def test_reinstall_preserves_previously_recorded_capabilities(tmp_path: Path) -> None:
    """``capabilities`` 是用户态（per-plugin 的 MCP 开关），重装不得把它抹掉。

    Kimi 自己 install 时写的是 ``existing?.capabilities``；我们必须照抄那个语义——
    有就原样保留（否则用户关掉的 MCP server 会被重装重新打开），没有就**不写这个键**。
    """
    marketplace_root = _build_marketplace_root(tmp_path / "marketplace")
    kimi_home = tmp_path / "kimi-home"
    install_to_codex.configure_kimi_plugin_registration(
        marketplace_root, kimi_code_home=kimi_home
    )

    registry_path = kimi_home / "plugins" / "installed.json"
    document = json.loads(registry_path.read_text(encoding="utf-8"))
    user_capabilities = {"mcpServers": {"some-server": {"enabled": False}}}
    document["plugins"][0]["capabilities"] = user_capabilities
    registry_path.write_text(json.dumps(document), encoding="utf-8")

    install_to_codex.configure_kimi_plugin_registration(
        marketplace_root, kimi_code_home=kimi_home
    )

    records = _read_registry(kimi_home)["plugins"]
    assert records[0]["capabilities"] == user_capabilities, records
