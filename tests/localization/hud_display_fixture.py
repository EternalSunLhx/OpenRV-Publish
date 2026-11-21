"""Assertions for a caller-owned visible RV instance with isolated preferences.

Call check_hud_localization() from RV's embedded Python after its UI is ready.
This checks the real Mu display helper and native popup bridge. It does not
register global callbacks, touch files, or execute user popup actions.
"""


def check_hud_localization():
    from PySide6 import QtCore, QtWidgets
    from rv import commands, runtime

    app = QtWidgets.QApplication.instance()
    language = app.property('rvInterfaceLanguage')
    assert language in ('en', 'zh_CN')
    expected_defaults = {
        'defaultLayout': ('Default Layout', '默认布局'),
        'defaultSequence': ('Default Sequence', '默认序列'),
        'defaultStack': ('Default Stack', '默认堆叠'),
    }
    def mu_true(expression):
        result = runtime.eval(expression, ['extra_commands', 'qt'])
        assert str(result).strip().lower() in ('true', '1'), (expression, result)

    checked_defaults = []
    for node, (original, chinese) in expected_defaults.items():
        raw_name = commands.getStringProperty(node + '.ui.name')[0]
        assert raw_name == original, (node, raw_name)
        expected = chinese if language == 'zh_CN' else original
        assert QtCore.QCoreApplication.translate('RvMenu', original) == expected
        mu_true('uiDisplayName("' + node + '") == qt.QCoreApplication.translate("RvMenu", "' + original + '");')
        assert commands.getStringProperty(node + '.ui.name')[0] == original
        renamed = '__i18n_user_name_' + node + '__'
        try:
            commands.setStringProperty(node + '.ui.name', [renamed], True)
            mu_true('uiDisplayName("' + node + '") == "' + renamed + '";')
        finally:
            commands.setStringProperty(node + '.ui.name', [original], True)
        checked_defaults.append(node)

    # Exercise the exact formatter used by drawIncomplete, including user data.
    current_view = commands.viewNode()
    original_inputs = commands.nodeConnections('defaultSequence', False)[0]
    try:
        commands.setNodeInputs('defaultSequence', [])
        commands.setViewNode('defaultSequence')
        mu_true('noImageFeedbackText("", "Default Sequence", "Empty") == '
                'qt.QCoreApplication.translate("RvMenu", "Default Sequence") + " -- " + '
                'qt.QCoreApplication.translate("RvMenu", "Empty");')
        mu_true('noImageFeedbackText("Image Info", "", "") == '
                'qt.QCoreApplication.translate("RvMenu", "%s -- No Image") % "Image Info";')
        mu_true('noImageFeedbackText("", "User View", "Unrecognized user text") == '
                '"User View -- Unrecognized user text";')
    finally:
        commands.setNodeInputs('defaultSequence', original_inputs)
        commands.setViewNode(current_view)

    def popup(scope):
        values = [widget for widget in app.allWidgets()
                  if isinstance(widget, QtWidgets.QMenu) and widget.objectName() == 'User Popup'
                  and widget.isVisible() and widget.property('rvLocalizedPopupScope') == scope]
        assert len(values) == 1, (scope, len(values))
        return values[0]

    # HUD is loaded lazily. Calling runtime.eval with an unloaded HUD module
    # is invalid; initialize its real registered widgets through the app event.
    # Toggling twice restores the original active state in this private session.
    commands.sendInternalEvent('toggle-hud-info-widget', '', 'localization-smoke')
    commands.sendInternalEvent('toggle-hud-info-widget', '', 'localization-smoke')
    checked_popups = []
    for mode, scope, sources in (
        ('imageinfo', 'HUD.ImageInfo', ['Image Info', 'Wrap Long Fields']),
        ('infoStrip', 'HUD.InfoStrip', ['Info Strip', 'Show Filename', 'Show UI Name', 'Scale with Resolution']),
    ):
        previously_active = mode in commands.activeModes()
        commands.activateMode(mode)
        try:
            commands.sendInternalEvent('pointer-3--push', '', 'localization-smoke')
            menu = popup(scope)
        finally:
            if not previously_active:
                commands.deactivateMode(mode)
        actions = [action for action in menu.actions() if not action.isSeparator()]
        assert [action.property('rvSourceText') for action in actions] == sources
        for action, source in zip(actions, sources):
            translated = QtCore.QCoreApplication.translate('RvMenu', source)
            assert action.text() == translated, (source, action.text(), translated)
            if language == 'zh_CN':
                assert translated != source, source
        checked_popups.append(scope)
        menu.close()

    # The same words can be user data. Untagged menus must remain verbatim.
    runtime.eval('popupMenuAtPoint(0, 0, newMenu(MenuItem[] {'
                 'menuText("Image Info"), menuText("Wrap Long Fields")}));',
                 ['commands', 'rvtypes', 'app_utils'])
    menu = popup('')
    actions = [action for action in menu.actions() if not action.isSeparator()]
    assert [action.text() for action in actions] == ['Image Info', 'Wrap Long Fields']
    assert [action.property('rvSourceText') for action in actions] == ['Image Info', 'Wrap Long Fields']
    menu.close()
    # Even explicit but unregistered scopes cannot translate dynamic data.
    runtime.eval('popupMenuLocalized(nil, newMenu(MenuItem[] {'
                 'menuText("Image Info"), menuText("Wrap Long Fields")}), "Probe.Dynamic");',
                 ['commands', 'rvtypes', 'app_utils'])
    menu = popup('Probe.Dynamic')
    assert [action.text() for action in menu.actions() if not action.isSeparator()] == ['Image Info', 'Wrap Long Fields']
    menu.close()
    return {'status': 'passed', 'language': language, 'defaultViewNames': checked_defaults,
            'localizedPopupScopes': checked_popups, 'ordinaryPopupPreserved': True, 'emptyFeedbackFormatter': 'passed', 'userStatusTextPreserved': True}
