// Copyright (c) 2026. SPDX-License-Identifier: Apache-2.0

#include <RvCommon/RvLanguage.h>
#include <RvCommon/RvMenuPaths.h>
#include <RvPackage/PackageManager.h>
#include <TwkGLText/TwkGLText.h>
#include <QAction>
#include <QActionGroup>
#include <QApplication>
#include <QCoreApplication>
#include <QDir>
#include <QFileInfo>
#include <QDebug>
#include <QLocale>
#include <QMenu>
#include <QMessageBox>
#include <QTranslator>

// Resource initialization must live outside namespace Rv.
static void initializeLanguageResources()
{
    Q_INIT_RESOURCE(RvLanguage);
}

namespace
{
    QString systemInterfaceLanguage;

    bool validLanguage(const QString& language)
    {
        return language == "system" || language == "en" || language == "zh_CN";
    }

    QString effectiveLanguage(const QString& language)
    {
        if (language == "system")
        {
            if (systemInterfaceLanguage.isEmpty())
                Rv::captureSystemInterfaceLanguage();
            return systemInterfaceLanguage;
        }
        return language;
    }

    void configureChineseOverlayFont()
    {
        QStringList candidates;
#if defined(PLATFORM_WINDOWS)
        const QDir fonts(QDir(qEnvironmentVariable("SystemRoot", "C:/Windows")).filePath("Fonts"));
        candidates << fonts.filePath("msyh.ttc") << fonts.filePath("msyhl.ttc")
                   << fonts.filePath("simhei.ttf") << fonts.filePath("simsun.ttc");
#elif defined(PLATFORM_DARWIN)
        candidates << QStringLiteral("/System/Library/Fonts/PingFang.ttc")
                   << QStringLiteral("/System/Library/Fonts/Supplemental/Songti.ttc")
                   << QStringLiteral("/System/Library/Fonts/STHeiti Light.ttc");
#else
        candidates << QStringLiteral("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
                   << QStringLiteral("/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc")
                   << QStringLiteral("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc");
#endif
        for (const QString& candidate : candidates)
        {
            if (!QFileInfo(candidate).isFile())
                continue;
            const QByteArray path = candidate.toUtf8();
            if (TwkGLText::GLtext::setInterfaceFontFile(path.constData()))
            {
                qApp->setProperty("rvOverlayFontFile", candidate);
                return;
            }
        }
        qWarning() << "No system Chinese font was available for OpenRV overlay text.";
    }

}

namespace Rv
{
    void captureSystemInterfaceLanguage()
    {
        // Called before main changes LANG/LC_ALL for safe numeric processing.
        // uiLanguages honors the platform UI preference, which can differ from
        // the region used to format numbers and dates.
        const QLocale systemLocale = QLocale::system();
        const QStringList languages = systemLocale.uiLanguages();
        const QLocale uiLocale(languages.isEmpty() ? systemLocale.name() : languages.first());
        systemInterfaceLanguage = uiLocale.language() == QLocale::Chinese ? QStringLiteral("zh_CN") : QStringLiteral("en");
    }

    QString interfaceLanguage()
    {
        RvSettings& settings = RvSettings::globalSettings();
        settings.beginGroup("General");
        const QString language = settings.value("interfaceLanguage", "system").toString();
        settings.endGroup();
        return validLanguage(language) ? language : QStringLiteral("system");
    }

    bool setInterfaceLanguage(const QString& language)
    {
        if (!validLanguage(language))
            return false;
        RvSettings& settings = RvSettings::globalSettings();
        if (!settings.isWritable())
            return false;
        settings.beginGroup("General");
        settings.setValue("interfaceLanguage", language);
        settings.endGroup();
        settings.sync();
        return true;
    }

    void initializeInterfaceLanguage()
    {
        static bool initialized = false;
        if (initialized)
            return;
        initialized = true;
        initializeLanguageResources();

        QString selection;
        if (qEnvironmentVariableIsSet("RV_UI_LANGUAGE"))
        {
            const QString overrideLanguage = qEnvironmentVariable("RV_UI_LANGUAGE");
            if (validLanguage(overrideLanguage))
            {
                selection = overrideLanguage;
                qApp->setProperty("rvInterfaceLanguageOverride", overrideLanguage);
            }
            else
                qWarning() << "Ignoring invalid RV_UI_LANGUAGE; expected system, en, or zh_CN:" << overrideLanguage;
        }

        if (selection.isEmpty())
            selection = interfaceLanguage();
        QString activeLanguage = effectiveLanguage(selection);
        if (activeLanguage == "zh_CN")
        {
            QTranslator* translator = new QTranslator(qApp);
            translator->setObjectName("rvApplicationTranslator");
            if (translator->load(":/openrv/translations/i18n_zh_CN.qm"))
            {
                // Install the application catalog last so its contexts win.
                // Prefer Qt translations shipped alongside this executable.
                QTranslator* qtTranslator = new QTranslator(qApp);
                qtTranslator->setObjectName("rvQtBaseTranslator");
                const QString shippedTranslations = QDir(QCoreApplication::applicationDirPath()).absoluteFilePath("../translations");
                if (qtTranslator->load(QLocale("zh_CN"), "qtbase", "_", shippedTranslations))
                    qApp->installTranslator(qtTranslator);
                else
                    delete qtTranslator;
                qApp->installTranslator(translator);
            }
            else
            {
                qWarning() << "Unable to load the OpenRV Chinese translation catalog; using English.";
                activeLanguage = "en";
                delete translator;
            }
        }
        qApp->setProperty("rvInterfaceLanguage", activeLanguage);
        if (activeLanguage == "zh_CN")
            configureChineseOverlayFont();
    }

    QMenu* createInterfaceLanguageMenu(QWidget* parent)
    {
        if (parent)
        {
            if (QMenu* existing = parent->findChild<QMenu*>("rvInterfaceLanguageMenu", Qt::FindDirectChildrenOnly))
                return existing;
        }
        QMenu* menu = new QMenu(QString::fromUtf8("Language / 语言"), parent);
        menu->setObjectName("rvInterfaceLanguageMenu");
        QActionGroup* group = new QActionGroup(menu);
        group->setExclusive(true);
        const QString selected = interfaceLanguage();
        const auto addLanguage = [menu, group, selected](const QString& label, const QString& language) {
            QAction* action = menu->addAction(label);
            action->setObjectName("rvInterfaceLanguage_" + language);
            action->setData(language);
            action->setCheckable(true);
            group->addAction(action);
            action->setChecked(language == selected);
        };
        addLanguage(QString::fromUtf8("System default / 跟随系统"), "system");
        addLanguage(QString::fromUtf8("English / 英语"), "en");
        addLanguage(QString::fromUtf8("简体中文 / Simplified Chinese"), "zh_CN");
        QObject::connect(menu, &QMenu::aboutToShow, menu, [group]() {
            const QString selected = interfaceLanguage();
            for (QAction* action : group->actions())
                action->setChecked(action->data().toString() == selected);
        });
        QObject::connect(group, &QActionGroup::triggered, menu, [menu](QAction* action) {
            const QString language = action->data().toString();
            if (language == interfaceLanguage())
                return;
            if (!setInterfaceLanguage(language))
            {
                QMessageBox::warning(menu, QString::fromUtf8("Language / 语言"),
                    QString::fromUtf8("The language preference could not be saved.\n无法保存界面语言设置。"));
                return;
            }
            QString message = QString::fromUtf8(
                "Language preference saved. Save your work, then restart OpenRV to apply it.\n"
                "界面语言设置已保存。请先保存工作，再重新启动 OpenRV 使其生效。");
            if (qApp->property("rvInterfaceLanguageOverride").isValid())
                message += QString::fromUtf8("\n\nRV_UI_LANGUAGE overrides this preference until it is removed.\n"
                    "当前 RV_UI_LANGUAGE 环境变量优先于此设置；移除该变量后才会使用保存的选择。");
            QMessageBox::information(menu, QString::fromUtf8("Restart required / 需要重新启动"), message);
        });
        return menu;
    }

    QString translateMenuText(const QString& sourceText, const QStringList& parentPath)
    {
        // Each shipped literal caption opts in by its full original menu path.
        // A known word in an unrelated dynamic/source/recent/profile submenu is
        // therefore still data, not a translatable interface label.
        QStringList path;
        for (const QString& section : parentPath)
            path.append(section.trimmed());
        path.append(sourceText.trimmed());
        if (!translatedStaticMenuPaths().contains(path.join('/')))
            return sourceText;

        const QByteArray source = sourceText.toUtf8();
        return QCoreApplication::translate("RvMenu", source.constData());
    }
}
