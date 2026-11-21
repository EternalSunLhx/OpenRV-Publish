// SPDX-License-Identifier: Apache-2.0
#include <QApplication>
#include <QCloseEvent>
#include <QDialog>
#include <QMainWindow>
#include <QPointer>
#include <QStringList>
#include <QtTest/QtTest>
#include <cassert>

namespace Rv
{
    using RvDocument = QMainWindow;
    class RvPreferences;
    class RvConsoleWindow;
    class RvNetworkDialog;
    class RvProfileManager;
    struct Session
    {
        static Session* current;
        RvDocument* document;
        static Session* currentSession() { return current; }
        void* opaquePointer() { return document; }
    };
    Session* Session::current = nullptr;

    // Keep the actual cached member declaration before RvPreferences is complete,
    // matching the production header's forward-declaration boundary.
    class RvApplication : public QObject
    {
    public:
        RvApplication();
        ~RvApplication();
        RvPreferences* prefDialog();
        void closeExistingAuxiliaryWindows();
        bool hasPreferences() const;
        RvConsoleWindow* console();
        RvNetworkDialog* networkWindow();
        RvProfileManager* profileManager();
#include <preference_member.inc>
#include <network_observer.inc>
        RvConsoleWindow* m_console = nullptr;
        RvNetworkDialog* m_networkDialog = nullptr;
        RvProfileManager* m_profileDialog = nullptr;
    };

    class Dialog : public QDialog
    {
    public:
        static int created;
        static QStringList events;
        QString kind;
        Dialog(QString name, QWidget* parent = nullptr) : QDialog(parent), kind(name) { ++created; }
        void closeEvent(QCloseEvent* event) override { events.append(kind); event->accept(); }
    };
    int Dialog::created = 0;
    QStringList Dialog::events;
    class RvPreferences : public Dialog
    {
    public:
        explicit RvPreferences(QWidget* parent) : Dialog(QStringLiteral("preferences"), parent) {}
    };
    class RvConsoleWindow : public Dialog
    {
    public:
        RvConsoleWindow() : Dialog(QStringLiteral("console")) {}
    };
    class RvNetworkDialog : public Dialog
    {
    public:
        bool running = true;
        RvNetworkDialog() : Dialog(QStringLiteral("network")) {}
        bool serverRunning() const { return running; }
        void shutdownServer() { events.append(QStringLiteral("shutdown-network")); running = false; }
    };
    class RvProfileManager : public Dialog
    {
    public:
        RvProfileManager() : Dialog(QStringLiteral("profile")) {}
    };
    RvApplication::RvApplication() : m_prefDialog() {}
    RvApplication::~RvApplication() { delete m_console; delete m_networkDialog; delete m_profileDialog; }
    bool RvApplication::hasPreferences() const { return m_prefDialog != nullptr; }
    RvConsoleWindow* RvApplication::console() { if (!m_console) m_console = new RvConsoleWindow; return m_console; }
    RvNetworkDialog* RvApplication::networkWindow() { if (!m_networkDialog) m_networkDialog = new RvNetworkDialog; return m_networkDialog; }
    RvProfileManager* RvApplication::profileManager() { if (!m_profileDialog) m_profileDialog = new RvProfileManager; return m_profileDialog; }

#include <production_methods.inc>
}

class PreferenceLifetimeTests : public QObject
{
    Q_OBJECT
private slots:
    void cacheClearsWhenTheOwningDocumentIsDeleted()
    {
        Rv::RvApplication application;
        auto* firstDocument = new Rv::RvDocument;
        Rv::Session firstSession{firstDocument};
        Rv::Session::current = &firstSession;
        QPointer<Rv::RvPreferences> firstPreferences(application.prefDialog());
        QCOMPARE(firstPreferences->parentWidget(), firstDocument);
        delete firstDocument;
        QVERIFY(firstPreferences.isNull());
        QVERIFY2(!application.hasPreferences(), "Destroyed document left the application's preference cache dangling");

        Rv::RvDocument secondDocument;
        Rv::Session secondSession{&secondDocument};
        Rv::Session::current = &secondSession;
        QCOMPARE(application.prefDialog()->parentWidget(), &secondDocument);
        QVERIFY(application.hasPreferences());
        Rv::Session::current = nullptr;
    }
#ifndef OPENRV_LIFETIME_LEGACY_NEGATIVE
    void shutdownDoesNotConstructPreviouslyUnopenedDialogs()
    {
        Rv::RvApplication application;
        const int before = Rv::Dialog::created;
        QVERIFY(!application.existingNetworkWindow());
        application.closeExistingAuxiliaryWindows();
        QCOMPARE(Rv::Dialog::created, before);
        QVERIFY(!application.hasPreferences());
        QVERIFY(!application.m_console && !application.m_profileDialog && !application.m_networkDialog);
    }
    void shutdownClosesOnlyExistingDialogsAndPreservesNetworkShutdownOrder()
    {
        Rv::RvApplication application;
        Rv::RvDocument document;
        Rv::Session session{&document};
        Rv::Session::current = &session;
        application.prefDialog(); application.console(); application.networkWindow(); application.profileManager();
        const int before = Rv::Dialog::created;
        Rv::Dialog::events.clear();
        application.closeExistingAuxiliaryWindows();
        QCOMPARE(Rv::Dialog::created, before);
        QCOMPARE(Rv::Dialog::events, QStringList({"shutdown-network", "network", "console", "preferences", "profile"}));
        QVERIFY(!application.m_networkDialog->serverRunning());
        Rv::Session::current = nullptr;
    }
#endif
};

int main(int argc, char** argv)
{
    qputenv("QT_QPA_PLATFORM", "offscreen");
    QApplication application(argc, argv);
    PreferenceLifetimeTests tests;
    return QTest::qExec(&tests, argc, argv);
}
#include "test_pref_dialog_lifetime.moc"
