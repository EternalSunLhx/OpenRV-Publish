"""Exercise real Qt extraction and manual translation updates on isolated fixtures.

Set OPENRV_TEST_LUPDATE to a trusted Qt lupdate executable to run the native
integration tests. Pure parsing tests always run. No OpenRV build is needed.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

TOOLS = Path(__file__).resolve().parents[2] / "tools/localization"
sys.path.insert(0, str(TOOLS))
import sync_sources as sync
import update_menu_paths as menus


class SourceSyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="source-sync-test-", dir=Path.cwd())
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name) / "candidate"
        self.report = Path(self.temporary.name) / "reports"
        self.repo.mkdir()
        for directory in (sync.COMMON / "RvCommon", sync.COMMON / "ui", Path("src/lib/app/mu_rvui"),
                          Path("src/plugins/rv-packages/example"), sync.CATALOG.parent):
            (self.repo / directory).mkdir(parents=True, exist_ok=True)
        self.write(sync.COMMON / "RvCommon/Widget.h", 'namespace Rv { class Widget { Q_OBJECT public: void show(); }; }')
        self.write(sync.COMMON / "Widget.cpp", '#include <RvCommon/Widget.h>\nnamespace Rv { void Widget::show() { tr("Known"); } }')
        root = ET.Element("TS", version="2.1", language="zh_CN", sourcelanguage="en_US")
        native = ET.SubElement(root, "context")
        ET.SubElement(native, "name").text = "Rv::Widget"
        message = ET.SubElement(native, "message")
        ET.SubElement(message, "source").text = "Known"
        ET.SubElement(message, "translation").text = "已有译文"
        context = ET.SubElement(root, "context")
        ET.SubElement(context, "name").text = "RvMenu"
        for text in ("File", "Stack", "Layout", "Align Start Frames", "Use Source Cut Info", "Automatically Retime Inputs", "Use Strict Frame Ranges"):
            message = ET.SubElement(context, "message")
            ET.SubElement(message, "source").text = text
            ET.SubElement(message, "translation").text = text
        sync.write_xml(self.repo / sync.CATALOG, root)

    def write(self, relative, content):
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def run_sync(self, apply=False):
        lupdate = os.environ.get("OPENRV_TEST_LUPDATE")
        if not lupdate:
            self.skipTest("OPENRV_TEST_LUPDATE is required for real Qt extraction")
        return sync.synchronize(self.repo, self.report, sync.find_lupdate(lupdate), apply)

    def test_explicit_popup_scope_keeps_static_paths_and_dynamic_data_separate(self):
        self.write("src/lib/app/mu_rvui/popup.mu", r'''
popupMenuLocalized(event, newMenu(MenuItem[] {
    menuText("Image Info"), menuItem("  Wrap Long Fields", nil),
    subMenu("Fixed", MenuItem[] { menuItem("Nested", nil) }),
    subMenu(userName, MenuItem[] { menuItem("Not promoted", nil) }),
    menuItem(filename, nil)
}), "HUD.ImageInfo");
popupMenu(event, newMenu(MenuItem[] { menuItem("Bare user popup", nil) }));
popupMenuLocalized(event, newMenu(MenuItem[] { menuItem("Dynamic scope", nil) }), "HUD." + suffix);
''')
        extracted = menus.extract_menus(self.repo)
        self.assertIn("@popup/HUD.ImageInfo", extracted["paths"])
        self.assertIn("@popup/HUD.ImageInfo/Wrap Long Fields", extracted["paths"])
        self.assertIn("@popup/HUD.ImageInfo/Fixed/Nested", extracted["paths"])
        self.assertIn("  Wrap Long Fields", extracted["captions"])
        self.assertNotIn(None, extracted["captions"])
        self.assertFalse(any("Not promoted" in path or "Bare user popup" in path or "Dynamic scope" in path for path in extracted["paths"]))

    def test_new_explicit_popup_caption_enters_manual_list(self):
        self.write("src/lib/app/mu_rvui/popup.mu", 'popupMenuLocalized(event, newMenu(MenuItem[] { menuItem("New popup caption", nil) }), "Fixture.Panel");')
        report = self.run_sync(apply=True)
        self.assertFalse(report["passed"])
        self.assertIn("New popup caption", {entry["source"] for entry in report["entries"]})
        self.assertNotIn("Fixture.Panel", {entry["source"] for entry in report["entries"]})

    def test_comments_urls_and_context_comments(self):
        text = '/* menuItem("Ignored") */ subMenu("File", [menuItem("URL // caption", nil)]); // trailing'
        self.write("src/lib/app/mu_rvui/test.mu", text)
        extracted = menus.extract_menus(self.repo)
        self.assertIn("File/URL // caption", extracted["paths"])
        self.assertNotIn("Ignored", extracted["captions"])
        self.write("src/plugins/rv-packages/example/test.mu", 'qt.QCoreApplication.translate("RvMenu", "A // B", "caption");')
        calls = list(sync.script_sources(self.repo))
        self.assertEqual(calls[0][0], ("RvMenu", "A // B", "caption"))

    def test_checkout_path_boundary(self):
        with self.assertRaises(RuntimeError):
            menus.checkout_path(self.repo, self.repo / "../outside")
        self.assertEqual(menus.checkout_path(self.repo, self.repo / "src/file"), self.repo / "src/file")

    def test_function_and_local_variable_menus_keep_their_mounted_paths(self):
        self.write("src/lib/app/mu_rvui/test.mu", r'''
\: buildViewMenu (Menu;)
{
    Menu a = newMenu(MenuItem[] {
        subMenu("Stereo", MenuItem[] {
            menuText("Stereo Viewing Mode"),
            menuItem("   Left Eye Only", nil)
        }),
        menuItem("Frame", nil),
        subMenu("Matte", MenuItem[] { menuItem("No Matte", nil) })
    });
    return a;
}
\: mainMenu (Menu;)
{
    Menu helpList = newMenu(MenuItem[] {
        menuText("Online Resources"), menuItem("   User Manual", nil)
    });
    helpList.push_back(menuItem("   Show Environment", nil));
    MenuItem[] extras = MenuItem[] { menuItem("Fixed Extra", nil) };
    extras.push_back(menuItem("Conditional Extra", nil));
    return newMenu(MenuItem[] {
        subMenu("View", buildViewMenu()), subMenu("Help", helpList), subMenu("Maya", extras)
    });
}
''')
        result = menus.extract_menus(self.repo)
        self.assertTrue({"View/Stereo/Left Eye Only", "View/Stereo/Stereo Viewing Mode", "View/Frame",
                         "View/Matte/No Matte", "Help/Online Resources", "Help/User Manual",
                         "Help/Show Environment", "Maya/Fixed Extra", "Maya/Conditional Extra"} <= result["paths"])
        self.assertIn("   Left Eye Only", result["captions"])
        self.assertNotIn("Left Eye Only", result["captions"])
        self.assertNotIn("Stereo", result["paths"])

    def test_dynamic_labels_and_other_scope_variables_are_not_promoted(self):
        self.write("src/lib/app/mu_rvui/test.mu", r'''
\: unused (Menu;)
{
    Menu data = newMenu(MenuItem[] { menuItem("Wrong Scope", nil) });
    return data;
}
\: buildView (Menu;)
{
    Menu data = newMenu(MenuItem[] {
        subMenu(profileName, MenuItem[] { menuItem("Do Not Promote", nil) }),
        menuItem(sourceName, nil), menuItem("Fixed Label", nil)
    });
    data.push_back(menuItem(deviceName, nil));
    return data;
}
\: mainMenu (Menu;)
{
    Menu data = newMenu(MenuItem[] { menuItem("Replaced Data", nil) });
    data = userSuppliedMenu();
    Menu cleared = newMenu(MenuItem[] { menuItem("Cleared Data", nil) });
    cleared.clear();
    return newMenu(MenuItem[] { subMenu("View", buildView()), subMenu("Help", data), subMenu("Tools", cleared) });
}
''')
        result = menus.extract_menus(self.repo)
        self.assertIn("View/Fixed Label", result["paths"])
        self.assertTrue({"Wrong Scope", "Do Not Promote", "Replaced Data", "Cleared Data"}.isdisjoint(result["captions"]))

    def test_new_builder_menu_caption_enters_manual_translation_list(self):
        self.write("src/lib/app/mu_rvui/test.mu", r'''
\: buildView (Menu;) { Menu a = newMenu(MenuItem[] { menuItem("Future Builder Caption", nil) }); return a; }
subMenu("File", buildView());
''')
        report = self.run_sync()
        self.assertFalse(report["passed"])
        self.assertEqual([entry["source"] for entry in report["entries"]], ["Future Builder Caption"])

    def test_only_shipped_literal_package_mode_paths_are_registered(self):
        self.write("src/plugins/rv-packages/example/PACKAGE", "modes:\n  - file: test.mu\n    menu: 'Tools/Annotation'\n")
        self.write("src/plugins/rv-packages/blank/PACKAGE", "modes:\n  - file: blank.mu\n    menu: ''\n")
        self.write("src/plugins/rv-packages/dynamic/PACKAGE", "modes:\n  - file: dynamic.mu\n    menu: Tools/${deviceName}\n")
        self.write("user-packages/customer/PACKAGE", "menu: Tools/Customer Name\n")
        result = menus.extract_menus(self.repo)
        self.assertIn("Tools/Annotation", result["paths"])
        self.assertIn("Annotation", result["captions"])
        self.assertNotIn("${deviceName}", result["captions"])
        self.assertNotIn("Customer Name", result["captions"])

    def test_new_qt_ui_mu_python_and_menu_keep_old_translation(self):
        self.write(sync.COMMON / "Widget.cpp", '#include <RvCommon/Widget.h>\nnamespace Rv { void Widget::show() { tr("Known"); tr("Future Native %1"); } }')
        self.write(sync.COMMON / "ui/NewDialog.ui", '<ui version="4.0"><class>NewDialog</class><widget class="QDialog" name="NewDialog"><property name="windowTitle"><string>Future Dialog</string></property></widget></ui>')
        self.write("src/lib/app/mu_rvui/test.mu", 'subMenu("File", [menuItem("Future Menu", nil)]); qt.QCoreApplication.translate("RvMenu", "Future Mu");')
        self.write("src/plugins/rv-packages/example/test.py", 'raise RuntimeError("Must never execute candidate Python")\nx = _rv_tr("Future Python")\n')
        before = (self.repo / sync.CATALOG).read_bytes()
        report = self.run_sync()
        self.assertFalse(report["passed"])
        self.assertEqual({entry["source"] for entry in report["entries"]},
                         {"Future Native %1", "Future Dialog", "Future Menu", "Future Mu", "Future Python"})
        self.assertEqual(before, (self.repo / sync.CATALOG).read_bytes())
        applied = self.run_sync(apply=True)
        self.assertEqual(applied["missingMessages"], 5)
        self.assertIn("已有译文", (self.repo / sync.CATALOG).read_text(encoding="utf-8"))
        first_report = (self.report / "missing-translations.json").read_bytes()
        first_candidate = (self.report / "candidate-i18n_zh.ts").read_bytes()
        self.run_sync(apply=True)
        self.assertEqual(first_report, (self.report / "missing-translations.json").read_bytes())
        self.assertEqual(first_candidate, (self.report / "candidate-i18n_zh.ts").read_bytes())
        self.assertNotIn(str(self.repo), first_report.decode())
        root = ET.parse(self.repo / sync.CATALOG).getroot()
        for translation in root.findall("context/message/translation"):
            if translation.get("type") == "unfinished":
                translation.attrib.pop("type")
                translation.text = "人工补译"
        # Preserve the placeholder on the one message which uses it.
        for message in root.findall("context/message"):
            if message.findtext("source") == "Future Native %1":
                message.find("translation").text = "新增原生文案 %1"
        sync.write_xml(self.repo / sync.CATALOG, root)
        self.assertTrue(self.run_sync()["passed"])

    def test_new_unwrapped_caption_blocks_until_translation_bridge_added(self):
        self.write("src/plugins/rv-packages/example/test.py", 'button.setToolTip("New unwrapped caption")\n')
        report = self.run_sync()
        self.assertFalse(report["passed"])
        self.assertEqual(report["missingMessages"], 0)
        self.assertEqual(report["needsTranslationBridge"][0]["source"], "New unwrapped caption")
        self.write("src/plugins/rv-packages/example/test.py", 'button.setToolTip(_rv_tr("New unwrapped caption"))\n')
        report = self.run_sync()
        self.assertFalse(report["passed"])
        self.assertFalse(report["needsTranslationBridge"])
        self.assertEqual(report["entries"][0]["source"], "New unwrapped caption")

    def test_new_outside_scope_form_requires_review(self):
        self.write("src/plugins/rv-packages/new_plugin/dialog.ui", '<ui><class>PluginDialog</class><widget><property name="windowTitle"><string>New Plugin</string></property></widget></ui>')
        report = self.run_sync()
        self.assertFalse(report["passed"])
        self.assertEqual(report["needsTranslationBridge"][0]["call"], "Qt form: PluginDialog")

    def test_new_native_plural_preserves_numerus_structure(self):
        self.write(sync.COMMON / "Widget.cpp", '#include <RvCommon/Widget.h>\nnamespace Rv { void Widget::show() { tr("Known"); tr("%n new frame(s)", nullptr, 3); } }')
        report = self.run_sync(apply=True)
        entry = next(x for x in report["entries"] if x["source"] == "%n new frame(s)")
        self.assertTrue(entry["numerus"])
        root = ET.parse(self.repo / sync.CATALOG).getroot()
        message = next(m for m in root.findall("context/message") if m.findtext("source") == "%n new frame(s)")
        self.assertEqual(message.get("numerus"), "yes")
        self.assertIsNotNone(message.find("translation/numerusform"))

    def test_candidate_tools_are_not_executed(self):
        marker = self.repo / "EXECUTED"
        self.write("tools/localization/update_menu_paths.py", f'from pathlib import Path; Path({str(marker)!r}).write_text("bad")')
        self.write("src/lib/app/RvCommon/RvCommon/RvMenuPaths.h", menus.render_header(menus.extract_menus(self.repo)["paths"]))
        result = subprocess.run([sys.executable, str(TOOLS / "check_translations.py"), "--repo", str(self.repo), "--report", str(self.report / "catalog.json")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
