// SPDX-License-Identifier: Apache-2.0
#ifndef RV_UPDATE_CHECKER_H
#define RV_UPDATE_CHECKER_H

#include <QByteArray>
#include <QDateTime>
#include <QObject>
#include <QPointer>
#include <QSet>
#include <QUrl>
#include <QVersionNumber>

class QDialog;
class QLabel;
class QMenu;
class QNetworkAccessManager;
class QNetworkReply;
class QPushButton;
class QTimer;
class QWidget;

namespace Rv
{
    namespace Updates
    {
        struct ReleaseVersion
        {
            QVersionNumber base;
            quint64 edition = 0;
            QString tag;
            quint64 buildNumber = 0;
            bool officialTag = false;
            bool isValid() const { return base.segmentCount() == 3 && !tag.isEmpty() && (officialTag || edition > 0); }
            QString displayVersion() const;
        };

        struct LocalVersion
        {
            QVersionNumber base;
            quint64 edition = 0;
            bool development = true;
            QString display;
            quint64 buildNumber = 0;
            bool officialTag = false;
        };

        enum class Offer { Invalid, Current, Older, Newer, PublishedForDevelopment };
        enum class ParseError { None, InvalidJson, InvalidRelease, InvalidVersion, InvalidUrl, InvalidAsset };

        struct ReleaseInfo
        {
            ReleaseVersion version;
            QUrl releaseUrl;
            QUrl downloadUrl; // Empty when this release has no installer for the compiled platform.
        };

        struct ParseResult
        {
            ReleaseInfo release;
            ParseError error = ParseError::None;
            bool isValid() const { return error == ParseError::None; }
        };

        ReleaseVersion parseReleaseVersion(const QString& tag);
        Offer compareRelease(const LocalVersion& local, const ReleaseVersion& published);
        ParseResult parseRelease(const QByteArray& json, const QString& platform, const QString& architecture);
        LocalVersion currentBuild();
        QString compiledPlatform();
        QString compiledArchitecture();
    }

    // One application-owned controller is reused by the rebuilt Help menus.
    class RvUpdateChecker : public QObject
    {
        Q_OBJECT
    public:
        explicit RvUpdateChecker(QObject* parent = nullptr, QNetworkAccessManager* manager = nullptr, int timeoutMs = 15000);
        ~RvUpdateChecker() override;
        void watchWindow(QWidget* window);
        void showDialog(QWidget* parent = nullptr);

    protected:
        bool eventFilter(QObject* watched, QEvent* event) override;
        virtual QDateTime currentDateTime() const;

    private slots:
        void checkAutomatically();

    private:
        QWidget* visibleWindow() const;
        bool claimAutomaticCheck(const QDate& date);
        void createDialog(QWidget* parent, bool automatic);
        void startRequest(bool automatic);
        void abortRequest();
        void finishRequest(QNetworkReply* reply);
        void setResult(const QString& state, const QString& text);
        void openLink(const QUrl& url);

        QPointer<QNetworkAccessManager> m_manager;
        QPointer<QNetworkReply> m_reply;
        QPointer<QDialog> m_dialog;
        QPointer<QLabel> m_status;
        QPointer<QLabel> m_latest;
        QPointer<QPushButton> m_close;
        QPointer<QPushButton> m_download;
        QPointer<QPushButton> m_notes;
        QSet<QWidget*> m_windows;
        QDate m_checkedDate;
        QDate m_requestDate;
        QTimer* m_dailyTimer;
        QTimer* m_timer = nullptr;
        int m_timeoutMs;
        QByteArray m_response;
        bool m_automaticRequest = false;
        bool m_timedOut = false;
        bool m_tooLarge = false;
        QUrl m_downloadUrl;
        QUrl m_releaseUrl;
    };

    void addUpdateCheckAction(QMenu* menu, QWidget* dialogParent);
}

#endif
