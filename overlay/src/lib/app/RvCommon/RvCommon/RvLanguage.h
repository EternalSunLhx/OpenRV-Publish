// Copyright (c) 2026. SPDX-License-Identifier: Apache-2.0
#ifndef __rv__RvLanguage__h__
#define __rv__RvLanguage__h__

#include <QString>
#include <QStringList>

class QMenu;
class QWidget;

namespace Rv
{
    // Language is independent of the numeric and media-processing locale.
    // Changes are persisted for the next launch; the running UI is not rebuilt.
    void captureSystemInterfaceLanguage();
    void initializeInterfaceLanguage();
    QString interfaceLanguage();
    bool setInterfaceLanguage(const QString& language);
    QMenu* createInterfaceLanguageMenu(QWidget* parent);

    // Display text only. Never change a TwkApp menu title or an action identifier.
    // Unknown/dynamic parent paths deliberately retain their original text.
    QString translateMenuText(const QString& sourceText, const QStringList& parentPath = QStringList());
}

#endif
