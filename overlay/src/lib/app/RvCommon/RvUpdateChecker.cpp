// SPDX-License-Identifier: Apache-2.0
#include <RvCommon/RvUpdateChecker.h>
#include <RvLocalizedBuild.h>

#include <QAction>
#include <QApplication>
#include <QDesktopServices>
#include <QDateTime>
#include <QDialog>
#include <QDialogButtonBox>
#include <QDir>
#include <QEvent>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonParseError>
#include <QLabel>
#include <QLockFile>
#include <QMenu>
#include <QMap>
#include <QSet>
#include <QSettings>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QPushButton>
#include <QRegularExpression>
#include <QSysInfo>
#include <QTimer>
#include <QVBoxLayout>
#include <QVariant>

namespace
{
    const qint64 maxResponseBytes = 2 * 1024 * 1024;
    const QString repositoryRoot = QStringLiteral("https://github.com/EternalSunLhx/OpenRV-Publish");

    bool exactUrl(const QJsonValue& value, const QUrl& expected)
    {
        if (!value.isString())
            return false;
        const QUrl url(value.toString(), QUrl::StrictMode);
        return url.isValid() && url.toEncoded(QUrl::FullyEncoded) == expected.toEncoded(QUrl::FullyEncoded);
    }

    bool validAssetSize(const QJsonValue& value)
    {
        const double size = value.toDouble(-1);
        return size > 0 && size <= 64.0 * 1024 * 1024 * 1024 && double(qint64(size)) == size;
    }

    QStringList officialAssetSuffixes(const QString& platform, const QString& architecture)
    {
        if (platform == QStringLiteral("Windows") && architecture == QStringLiteral("x64"))
            return {QStringLiteral("-Windows-x64-Setup.exe"), QStringLiteral("-Windows-x64-Portable.zip"),
                    QStringLiteral("-Windows-x64.build.json"), QStringLiteral("-Windows-x64.sha256.txt")};
        if (platform == QStringLiteral("macOS")
            && (architecture == QStringLiteral("arm64") || architecture == QStringLiteral("x86_64")))
        {
            const QString prefix = QStringLiteral("-macOS-") + architecture;
            return {prefix + QStringLiteral(".dmg"), prefix + QStringLiteral(".zip"),
                    prefix + QStringLiteral(".build.json"), prefix + QStringLiteral(".sha256.txt")};
        }
        return {};
    }

    QString expectedAssetName(const QString& tag, const QString& platform, const QString& architecture)
    {
        const QString prefix = QStringLiteral("OpenRV-") + tag.mid(1);
        if (platform == QStringLiteral("Windows") && architecture == QStringLiteral("x64"))
            return prefix + QStringLiteral("-Windows-x64-Setup.exe");
        if (platform == QStringLiteral("macOS")
            && (architecture == QStringLiteral("arm64") || architecture == QStringLiteral("x86_64")))
            return prefix + QStringLiteral("-macOS-") + architecture + QStringLiteral(".dmg");
        return QString();
    }

    QStringList publicationAssetNames(const QString& tag)
    {
        const QString prefix = QStringLiteral("OpenRV-") + tag.mid(1);
        QStringList names;
        for (const QString& suffix : {QStringLiteral("-Windows-x64-Setup.exe"), QStringLiteral("-Windows-x64-Portable.zip"),
                                      QStringLiteral("-Windows-x64.sha256.txt"), QStringLiteral("-macOS-x86_64.dmg"),
                                      QStringLiteral("-macOS-x86_64.zip"), QStringLiteral("-macOS-x86_64.sha256.txt"),
                                      QStringLiteral("-macOS-arm64.dmg"), QStringLiteral("-macOS-arm64.zip"),
                                      QStringLiteral("-macOS-arm64.sha256.txt"), QStringLiteral("-source.provenance.json"),
                                      QStringLiteral("-generated-localization.patch.zip"), QStringLiteral("-source.sha256.txt")})
            names.append(prefix + suffix);
        return names;
    }

    bool publicationMarker(const QString& body, const QString& tag, quint64& build, QJsonObject& hashes)
    {
        const QString identifier = QStringLiteral("openrv-publication");
        if (body.count(identifier, Qt::CaseInsensitive) != 1)
            return false;
        static const QRegularExpression line(QStringLiteral("(?:\\A|\\n)<!-- openrv-publication: ([^\\r\\n]+) -->(?=\\r?\\n|\\z)"));
        const QRegularExpressionMatch match = line.match(body);
        if (!match.hasMatch() || match.captured(1).toUtf8().size() > 16384)
            return false;
        QJsonParseError error;
        const QJsonDocument document = QJsonDocument::fromJson(match.captured(1).toUtf8(), &error);
        if (error.error != QJsonParseError::NoError || !document.isObject())
            return false;
        const QJsonObject marker = document.object();
        if (marker.size() != 4 || !marker.value(QStringLiteral("schema")).isDouble()
            || marker.value(QStringLiteral("schema")).toDouble() != 1
            || marker.value(QStringLiteral("tag")).toString() != tag
            || !marker.value(QStringLiteral("buildNumber")).isString()
            || !marker.value(QStringLiteral("assets")).isObject())
            return false;
        static const QRegularExpression counter(QStringLiteral("\\A[1-9][0-9]*\\z"));
        const QString number = marker.value(QStringLiteral("buildNumber")).toString();
        bool valid = false;
        build = number.toULongLong(&valid);
        if (!counter.match(number).hasMatch() || !valid || !build)
            return false;
        hashes = marker.value(QStringLiteral("assets")).toObject();
        const QStringList names = publicationAssetNames(tag);
        if (hashes.size() != names.size())
            return false;
        static const QRegularExpression digest(QStringLiteral("\\Asha256:[0-9a-f]{64}\\z"));
        for (const QString& name : names)
        {
            const QJsonValue hash = hashes.value(name);
            if (!hash.isString() || !digest.match(hash.toString()).hasMatch())
                return false;
        }
        // QJsonObject coalesces duplicate keys. In this finite schema all
        // values are constrained strings/numbers except the one assets map;
        // count raw member tokens too, so repeated keys (including escaped
        // spellings) cannot silently replace an earlier value.
        static const QRegularExpression member(QStringLiteral("\"(?:[^\"\\\\]|\\\\.)*\"\\s*:"));
        auto members = member.globalMatch(match.captured(1));
        int count = 0;
        while (members.hasNext())
        {
            members.next();
            ++count;
        }
        return count == 4 + names.size();
    }
}

namespace Rv
{
    namespace Updates
    {
        ReleaseVersion parseReleaseVersion(const QString& tag)
        {
            static const QRegularExpression format(QStringLiteral("\\Av(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)(?:-zh\\.([1-9][0-9]*))?\\z"));
            const QRegularExpressionMatch match = format.match(tag);
            if (!match.hasMatch())
                return {};
            int parts[3];
            for (int index = 0; index < 3; ++index)
            {
                bool valid = false;
                parts[index] = match.captured(index + 1).toInt(&valid);
                if (!valid || parts[index] > 65535)
                    return {};
            }
            quint64 edition = 0;
            const bool official = match.captured(4).isEmpty();
            if (!official)
            {
                bool valid = false;
                edition = match.captured(4).toULongLong(&valid);
                if (!valid || edition == 0)
                    return {};
            }
            return {QVersionNumber(parts[0], parts[1], parts[2]), edition, tag, 0, official};
        }

        QString ReleaseVersion::displayVersion() const
        {
            if (!officialTag)
                return tag;
            return base.toString() + (buildNumber ? QStringLiteral("+build.%1").arg(buildNumber) : QString());
        }

        Offer compareRelease(const LocalVersion& local, const ReleaseVersion& published)
        {
            if (local.base.segmentCount() != 3 || !published.isValid()
                || (!local.development && (local.officialTag ? local.buildNumber == 0 : local.edition == 0)))
                return Offer::Invalid;
            const int baseComparison = QVersionNumber::compare(published.base, local.base);
            if (baseComparison > 0)
                return Offer::Newer;
            if (baseComparison < 0)
                return Offer::Older;
            if (local.development)
                return Offer::PublishedForDevelopment;
            if (local.officialTag)
            {
                // A legacy edition cannot replace an official build of the same
                // version. Build counters and historical -zh editions are distinct.
                if (!published.officialTag)
                    return Offer::Older;
                if (published.buildNumber > local.buildNumber)
                    return Offer::Newer;
                return published.buildNumber == local.buildNumber ? Offer::Current : Offer::Older;
            }
            if (published.officialTag)
                return Offer::Newer; // Migrate an existing -zh installation to the official-tag build.
            if (published.edition > local.edition)
                return Offer::Newer;
            return published.edition == local.edition ? Offer::Current : Offer::Older;
        }

        ParseResult parseRelease(const QByteArray& json, const QString& platform, const QString& architecture)
        {
            if (json.size() > maxResponseBytes)
                return {{}, ParseError::InvalidJson};
            QJsonParseError error;
            const QJsonDocument document = QJsonDocument::fromJson(json, &error);
            if (error.error != QJsonParseError::NoError || !document.isObject())
                return {{}, ParseError::InvalidJson};
            const QJsonObject object = document.object();
            if (!object.value(QStringLiteral("draft")).isBool() || object.value(QStringLiteral("draft")).toBool()
                || !object.value(QStringLiteral("prerelease")).isBool() || object.value(QStringLiteral("prerelease")).toBool())
                return {{}, ParseError::InvalidRelease};
            if (!object.value(QStringLiteral("published_at")).isString()
                || !QDateTime::fromString(object.value(QStringLiteral("published_at")).toString(), Qt::ISODate).isValid())
                return {{}, ParseError::InvalidRelease};
            const ReleaseVersion version = parseReleaseVersion(object.value(QStringLiteral("tag_name")).toString());
            if (!version.isValid())
                return {{}, ParseError::InvalidVersion};
            const QUrl releaseUrl(repositoryRoot + QStringLiteral("/releases/tag/") + version.tag);
            if (!exactUrl(object.value(QStringLiteral("html_url")), releaseUrl))
                return {{}, ParseError::InvalidUrl};
            const QJsonValue assetsValue = object.value(QStringLiteral("assets"));
            // Same-tag builds retain earlier assets. The response remains bounded
            // to 2 MiB before JSON parsing; this also limits metadata iteration.
            if (!assetsValue.isArray() || assetsValue.toArray().size() > 4096)
                return {{}, ParseError::InvalidAsset};
            ReleaseInfo release{version, releaseUrl, QUrl()};
            const QString expected = expectedAssetName(version.tag, platform, architecture);
            const QJsonValue body = object.value(QStringLiteral("body"));
            if (!body.isUndefined() && !body.isNull() && !body.isString())
                return {{}, ParseError::InvalidRelease};
            if (body.toString().contains(QStringLiteral("openrv-publication"), Qt::CaseInsensitive))
            {
                quint64 build = 0;
                QJsonObject hashes;
                if (!version.officialTag || !publicationMarker(body.toString(), version.tag, build, hashes)
                    || assetsValue.toArray().size() != hashes.size())
                    return {{}, ParseError::InvalidAsset};
                QSet<QString> seen;
                for (const QJsonValue& value : assetsValue.toArray())
                {
                    if (!value.isObject())
                        return {{}, ParseError::InvalidAsset};
                    const QJsonObject asset = value.toObject();
                    const QString name = asset.value(QStringLiteral("name")).toString();
                    const QUrl url(repositoryRoot + QStringLiteral("/releases/download/") + version.tag + '/' + name);
                    if (!hashes.contains(name) || seen.contains(name)
                        || asset.value(QStringLiteral("state")).toString() != QStringLiteral("uploaded")
                        || !validAssetSize(asset.value(QStringLiteral("size")))
                        || asset.value(QStringLiteral("digest")) != hashes.value(name)
                        || !exactUrl(asset.value(QStringLiteral("browser_download_url")), url))
                        return {{}, ParseError::InvalidAsset};
                    seen.insert(name);
                    if (!expected.isEmpty() && name == expected)
                        release.downloadUrl = url;
                }
                release.version.buildNumber = build;
                return {release, ParseError::None};
            }
            const QStringList suffixes = officialAssetSuffixes(platform, architecture);
            QStringList escapedSuffixes;
            for (const QString& suffix : suffixes)
                escapedSuffixes.append(QRegularExpression::escape(suffix));
            const QRegularExpression officialAssetFormat(QStringLiteral("\\A")
                + QRegularExpression::escape(QStringLiteral("OpenRV-") + version.base.toString() + QStringLiteral("-build."))
                + QStringLiteral("([1-9][0-9]*)(") + escapedSuffixes.join('|') + QStringLiteral(")\\z"));
            QSet<QString> seenNames;
            QMap<quint64, QSet<QString>> buildAssets;
            QMap<quint64, QUrl> buildDownloads;
            bool foundLegacy = false;
            for (const QJsonValue& value : assetsValue.toArray())
            {
                if (!value.isObject())
                    return {{}, ParseError::InvalidAsset};
                const QJsonObject asset = value.toObject();
                const QString name = asset.value(QStringLiteral("name")).toString();
                if (!version.officialTag)
                {
                    if (expected.isEmpty() || name != expected)
                        continue;
                    const QUrl downloadUrl(repositoryRoot + QStringLiteral("/releases/download/") + version.tag + '/' + expected);
                    if (foundLegacy || asset.value(QStringLiteral("state")).toString() != QStringLiteral("uploaded")
                        || !validAssetSize(asset.value(QStringLiteral("size")))
                        || !exactUrl(asset.value(QStringLiteral("browser_download_url")), downloadUrl))
                        return {{}, ParseError::InvalidAsset};
                    foundLegacy = true;
                    release.downloadUrl = downloadUrl;
                    continue;
                }
                if (suffixes.isEmpty())
                    continue;
                const QRegularExpressionMatch match = officialAssetFormat.match(name);
                if (!match.hasMatch())
                    continue;
                bool validBuild = false;
                const quint64 build = match.captured(1).toULongLong(&validBuild);
                if (!validBuild || build == 0 || seenNames.contains(name))
                    return {{}, ParseError::InvalidAsset};
                seenNames.insert(name);
                const QUrl downloadUrl(repositoryRoot + QStringLiteral("/releases/download/") + version.tag + '/' + name);
                if (!exactUrl(asset.value(QStringLiteral("browser_download_url")), downloadUrl))
                    return {{}, ParseError::InvalidAsset};
                // While a newer platform set is still uploading, the previous
                // complete build remains available. Never mix its companions.
                if (asset.value(QStringLiteral("state")).toString() != QStringLiteral("uploaded")
                    || !validAssetSize(asset.value(QStringLiteral("size"))))
                    continue;
                const QString suffix = match.captured(2);
                buildAssets[build].insert(suffix);
                if (suffix == suffixes.first())
                    buildDownloads[build] = downloadUrl;
            }
            auto build = buildAssets.constEnd();
            while (build != buildAssets.constBegin())
            {
                --build;
                if (build.value().size() != suffixes.size())
                    continue;
                release.version.buildNumber = build.key();
                release.downloadUrl = buildDownloads.value(build.key());
                break;
            }
            return {release, ParseError::None};
        }

        LocalVersion currentBuild()
        {
            LocalVersion local;
            local.base = QVersionNumber(RV_LOCALIZED_MAJOR, RV_LOCALIZED_MINOR, RV_LOCALIZED_PATCH);
            local.display = QStringLiteral(RV_LOCALIZED_VERSION);
            const ReleaseVersion release = parseReleaseVersion(QStringLiteral(RV_LOCALIZED_RELEASE_TAG));
            if (release.isValid() && release.base == local.base)
            {
                if (release.officialTag)
                {
                    if (RV_LOCALIZED_BUILD_NUMBER > 0)
                    {
                        local.buildNumber = RV_LOCALIZED_BUILD_NUMBER;
                        local.officialTag = true;
                        local.development = false;
                    }
                }
                else
                {
                    local.edition = release.edition;
                    local.development = false;
                }
            }
            return local;
        }

        QString compiledPlatform()
        {
#if defined(Q_OS_WIN)
            return QStringLiteral("Windows");
#elif defined(Q_OS_MACOS)
            return QStringLiteral("macOS");
#else
            return QStringLiteral("unsupported");
#endif
        }

        QString compiledArchitecture()
        {
            const QString architecture = QSysInfo::buildCpuArchitecture();
#if defined(Q_OS_WIN)
            return architecture == QStringLiteral("x86_64") ? QStringLiteral("x64") : architecture;
#else
            return architecture == QStringLiteral("aarch64") ? QStringLiteral("arm64") : architecture;
#endif
        }
    }

    RvUpdateChecker::RvUpdateChecker(QObject* parent, QNetworkAccessManager* manager, int timeoutMs)
        : QObject(parent), m_manager(manager), m_dailyTimer(new QTimer(this)), m_timer(new QTimer(this)),
          m_timeoutMs(qBound(1, timeoutMs, 15000))
    {
        setObjectName(QStringLiteral("rvUpdateChecker"));
        m_dailyTimer->setSingleShot(true);
        m_dailyTimer->setTimerType(Qt::PreciseTimer);
        connect(m_dailyTimer, &QTimer::timeout, this, &RvUpdateChecker::checkAutomatically);
        connect(qApp, &QGuiApplication::applicationStateChanged, this, [this](Qt::ApplicationState state) {
            if (state == Qt::ApplicationActive && !m_windows.isEmpty())
                m_dailyTimer->start(0);
        });
        m_timer->setSingleShot(true);
        connect(m_timer, &QTimer::timeout, this, [this]() {
            if (m_reply)
            {
                m_timedOut = true;
                m_reply->abort();
            }
        });
    }

    RvUpdateChecker::~RvUpdateChecker() { abortRequest(); }

    void RvUpdateChecker::watchWindow(QWidget* window)
    {
        if (!window || m_windows.contains(window) || qgetenv("RV_DISABLE_AUTOMATIC_UPDATE_CHECK") == "1")
            return;
        m_windows.insert(window);
        window->installEventFilter(this);
        connect(window, &QObject::destroyed, this, [this, window]() { m_windows.remove(window); });
        if (window->isVisible())
            m_dailyTimer->start(0);
    }

    bool RvUpdateChecker::eventFilter(QObject* watched, QEvent* event)
    {
        if (event->type() == QEvent::Show || event->type() == QEvent::WindowActivate
            || event->type() == QEvent::WindowStateChange)
            m_dailyTimer->start(0);
        return QObject::eventFilter(watched, event);
    }

    QWidget* RvUpdateChecker::visibleWindow() const
    {
        QWidget* active = QApplication::activeWindow();
        if (m_windows.contains(active) && active->isVisible() && !active->isMinimized())
            return active;
        for (QWidget* window : m_windows)
            if (window->isVisible() && !window->isMinimized())
                return window;
        return nullptr;
    }

    QDateTime RvUpdateChecker::currentDateTime() const { return QDateTime::currentDateTime(); }

    bool RvUpdateChecker::claimAutomaticCheck(const QDate& date)
    {
        if (m_checkedDate == date)
            return false;
        QSettings settings(QSettings::IniFormat, QSettings::UserScope, QStringLiteral("OpenRV-Publish"),
                           QStringLiteral("UpdateChecker"));
        settings.setFallbacksEnabled(false);
        if (!QFileInfo(settings.fileName()).absoluteDir().mkpath(QStringLiteral(".")))
            return false;
        // Serialize only the read/claim/write. The network request runs outside this lock.
        QLockFile lock(settings.fileName() + QStringLiteral(".daily.lock"));
        if (!lock.tryLock(0))
            return false;
        settings.sync();
        if (settings.status() != QSettings::NoError)
            return false;
        const QString key = QStringLiteral("lastAutomaticCheckDate");
        const QString today = date.toString(Qt::ISODate);
        if (settings.value(key).toString() == today)
        {
            m_checkedDate = date;
            return false;
        }
        settings.setValue(key, today);
        settings.sync();
        if (settings.status() != QSettings::NoError)
            return false;
        m_checkedDate = date;
        return true;
    }

    void RvUpdateChecker::checkAutomatically()
    {
        if (QCoreApplication::closingDown() || qgetenv("RV_DISABLE_AUTOMATIC_UPDATE_CHECK") == "1" || !visibleWindow())
            return;
        const QDateTime now = currentDateTime();
        const QDateTime tomorrow = now.date().addDays(1).startOfDay();
        // A bounded recheck also observes clock changes or resume without a window activation.
        m_dailyTimer->start(int(qBound(qint64(1), now.msecsTo(tomorrow), qint64(15 * 60 * 1000))));
        // A manual request from today can serve both checks. Yesterday's response
        // cannot claim today; finishRequest will recheck when that request ends.
        if (m_reply && m_requestDate != now.date())
            return;
        if (claimAutomaticCheck(now.date()) && !m_reply)
            startRequest(true);
    }

    void RvUpdateChecker::abortRequest()
    {
        m_timer->stop();
        if (m_reply)
        {
            QNetworkReply* reply = m_reply.data();
            m_reply.clear();
            reply->disconnect(this);
            reply->abort();
            reply->deleteLater();
        }
    }

    void RvUpdateChecker::showDialog(QWidget* parent)
    {
        if (m_dialog)
        {
            m_dialog->show();
            m_dialog->raise();
            m_dialog->activateWindow();
            return;
        }
        m_automaticRequest = false;
        createDialog(parent, false);
        m_dialog->show();
        if (!m_reply)
            startRequest(false);
    }

    void RvUpdateChecker::createDialog(QWidget* parent, bool automatic)
    {
        QDialog* dialog = new QDialog(parent);
        m_dialog = dialog;
        dialog->setAttribute(Qt::WA_DeleteOnClose);
        if (automatic)
            dialog->setAttribute(Qt::WA_ShowWithoutActivating);
        dialog->setObjectName(QStringLiteral("rvUpdateDialog"));
        dialog->setWindowTitle(tr("Check for Updates"));
        dialog->setMinimumWidth(430);
        const Updates::LocalVersion local = Updates::currentBuild();
        dialog->setProperty("rvCurrentVersion", local.display);
        dialog->setProperty("rvAutomaticUpdate", automatic);
        dialog->setProperty("rvUpdateResult", QStringLiteral("checking"));
        QVBoxLayout* layout = new QVBoxLayout(dialog);
        QLabel* current = new QLabel(tr("Current version: %1").arg(local.display), dialog);
        current->setTextFormat(Qt::PlainText);
        layout->addWidget(current);
        m_latest = new QLabel(dialog);
        m_latest->setObjectName(QStringLiteral("rvLatestUpdateVersion"));
        m_latest->setTextFormat(Qt::PlainText);
        layout->addWidget(m_latest);
        m_status = new QLabel(tr("Checking published releases..."), dialog);
        m_status->setObjectName(QStringLiteral("rvUpdateStatus"));
        m_status->setTextFormat(Qt::PlainText);
        m_status->setWordWrap(true);
        layout->addWidget(m_status);
        QDialogButtonBox* buttons = new QDialogButtonBox(dialog);
        m_download = buttons->addButton(tr("Download installer"), QDialogButtonBox::ActionRole);
        m_download->setObjectName(QStringLiteral("rvUpdateDownloadButton"));
        m_download->setAutoDefault(false);
        m_download->hide();
        m_notes = buttons->addButton(tr("Release notes"), QDialogButtonBox::ActionRole);
        m_notes->setObjectName(QStringLiteral("rvUpdateReleaseNotesButton"));
        m_notes->setAutoDefault(false);
        m_notes->hide();
        m_close = buttons->addButton(tr("Cancel"), QDialogButtonBox::RejectRole);
        m_close->setObjectName(QStringLiteral("rvUpdateCloseButton"));
        m_close->setDefault(true);
        layout->addWidget(buttons);
        connect(m_download, &QPushButton::clicked, this, [this]() { openLink(m_downloadUrl); });
        connect(m_notes, &QPushButton::clicked, this, [this]() { openLink(m_releaseUrl); });
        connect(buttons, &QDialogButtonBox::rejected, dialog, &QDialog::reject);
        connect(dialog, &QDialog::finished, this, [this, dialog, automatic](int) {
            if (m_dialog == dialog)
            {
                dialog->disconnect(this);
                abortRequest();
                m_dialog.clear();
                // "Later" can dismiss a prompt that stayed open across midnight.
                if (automatic)
                    claimAutomaticCheck(currentDateTime().date());
                if (!m_windows.isEmpty())
                    m_dailyTimer->start(0);
            }
        });
        // A document closing also destroys its child dialog; cancellation must not
        // leave a request alive or let its result reach a later dialog instance.
        connect(dialog, &QObject::destroyed, this, [this, dialog]() {
            if (m_dialog.data() == dialog || m_dialog.isNull())
                abortRequest();
        });
    }

    void RvUpdateChecker::startRequest(bool automatic)
    {
        if (!m_manager)
            m_manager = new QNetworkAccessManager(this);
        m_automaticRequest = automatic;
        m_requestDate = currentDateTime().date();
        m_response.clear();
        m_timedOut = m_tooLarge = false;
        if (!automatic)
        {
            m_downloadUrl.clear();
            m_releaseUrl.clear();
        }
        QNetworkRequest request(QUrl(QStringLiteral("https://api.github.com/repos/EternalSunLhx/OpenRV-Publish/releases/latest")));
        request.setAttribute(QNetworkRequest::RedirectPolicyAttribute, QNetworkRequest::ManualRedirectPolicy);
        request.setAttribute(QNetworkRequest::CacheLoadControlAttribute, QNetworkRequest::AlwaysNetwork);
        request.setRawHeader("Accept", "application/vnd.github+json");
        request.setRawHeader("Accept-Encoding", "identity");
        request.setRawHeader("X-GitHub-Api-Version", "2026-03-10");
        request.setRawHeader("User-Agent", "OpenRV-Publish-update-check/" + Updates::currentBuild().display.toUtf8());
        QNetworkReply* reply = m_manager->get(request);
        m_reply = reply;
        reply->setReadBufferSize(maxResponseBytes + 1);
        connect(reply, &QIODevice::readyRead, this, [this, reply]() {
            if (m_reply != reply)
                return;
            m_response += reply->readAll();
            if (m_response.size() > maxResponseBytes)
            {
                m_tooLarge = true;
                reply->abort();
            }
        });
        connect(reply, &QNetworkReply::finished, this, [this, reply]() { finishRequest(reply); });
        m_timer->start(m_timeoutMs);
    }

    void RvUpdateChecker::setResult(const QString& state, const QString& text)
    {
        if (!m_dialog)
            return;
        m_dialog->setProperty("rvUpdateResult", state);
        m_status->setText(text);
        m_close->setText(m_dialog->property("rvAutomaticUpdate").toBool() ? tr("Later") : tr("Close"));
        m_dialog->adjustSize();
    }

    void RvUpdateChecker::finishRequest(QNetworkReply* reply)
    {
        if (m_reply != reply)
        {
            reply->deleteLater();
            return;
        }
        m_timer->stop();
        m_response += reply->readAll();
        m_reply.clear();
        reply->deleteLater();
        const bool previousDay = m_requestDate != currentDateTime().date();
        if (!m_windows.isEmpty() && previousDay)
            m_dailyTimer->start(0);
        const bool automatic = m_automaticRequest;
        if (automatic && previousDay)
            return;
        // A result window may have been left open from a previous day.
        // An unsuccessful background request must not replace its contents.
        const auto reportFailure = [this, automatic](const QString& state, const QString& message) {
            if (!automatic)
                setResult(state, message);
        };
        if (m_timedOut)
            return reportFailure(QStringLiteral("error"), tr("The update check timed out. Try again later."));
        if (m_tooLarge || m_response.size() > maxResponseBytes)
            return reportFailure(QStringLiteral("error"), tr("The release response was too large."));
        const int status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
        if (status == 404)
            return reportFailure(QStringLiteral("no-release"), tr("This repository has no published release yet."));
        if (status == 403 || status == 429)
            return reportFailure(QStringLiteral("error"), tr("GitHub rejected the request or its rate limit was reached. Try again later."));
        if (reply->error() != QNetworkReply::NoError)
            return reportFailure(QStringLiteral("error"), tr("The update check failed. Check your Internet connection and try again."));
        if (status != 200)
            return reportFailure(QStringLiteral("error"), tr("The update server returned an unexpected response (HTTP %1).").arg(status));
        const Updates::ParseResult parsed = Updates::parseRelease(m_response, Updates::compiledPlatform(), Updates::compiledArchitecture());
        if (!parsed.isValid())
            return reportFailure(QStringLiteral("error"), tr("The release information could not be verified. No download was opened."));
        const Updates::ReleaseInfo& release = parsed.release;
        const Updates::Offer offer = Updates::compareRelease(Updates::currentBuild(), release.version);
        const bool showAutomaticDialog = automatic && !m_dialog;
        if (automatic)
        {
            QWidget* parent = visibleWindow();
            if (!parent || offer != Updates::Offer::Newer || release.downloadUrl.isEmpty())
                return;
            if (!m_dialog)
                createDialog(parent, true);
        }
        if (!m_dialog)
            return;
        const QString latestDisplay = release.version.displayVersion();
        m_latest->setText(tr("Latest published version: %1").arg(latestDisplay));
        m_dialog->setProperty("rvLatestVersion", latestDisplay);
        m_dialog->setProperty("rvLatestReleaseTag", release.version.tag);
        m_dialog->setProperty("rvLatestBuildNumber", QVariant::fromValue(release.version.buildNumber));
        m_releaseUrl = release.releaseUrl;
        m_downloadUrl = release.downloadUrl;
        m_dialog->setProperty("rvReleaseUrl", m_releaseUrl);
        m_dialog->setProperty("rvDownloadUrl", m_downloadUrl);
        m_notes->show();
        if (offer == Updates::Offer::Current)
            return setResult(QStringLiteral("current"), tr("You are running the latest published version."));
        if (offer == Updates::Offer::Older)
            return setResult(QStringLiteral("older"), tr("Your build is newer than the repository's latest published version."));
        if (offer == Updates::Offer::Invalid)
            return setResult(QStringLiteral("error"), tr("The current build version could not be verified."));
        if (m_downloadUrl.isEmpty())
            return setResult(QStringLiteral("no-package"), tr("This release has no installer for this platform. See the release notes for available files."));
        m_download->show();
        if (offer == Updates::Offer::PublishedForDevelopment)
        {
            m_download->setText(tr("Download published version"));
            return setResult(QStringLiteral("development-alternative"), tr("You are running a development build. A published version of the same base version is available."));
        }
        setResult(QStringLiteral("newer"), tr("A newer published version is available. Download its installer to upgrade."));
        if (showAutomaticDialog)
            m_dialog->show();
    }

    void RvUpdateChecker::openLink(const QUrl& url)
    {
        // These are exact URLs constructed and validated by parseRelease().
        // Opening a browser never executes or replaces the installed application.
        if (url.isEmpty() || !QDesktopServices::openUrl(url))
            setResult(QStringLiteral("error"), tr("The download page could not be opened. Try the release notes or open the repository in your browser."));
    }

    void addUpdateCheckAction(QMenu* menu, QWidget* dialogParent)
    {
        if (!menu || menu->findChild<QAction*>(QStringLiteral("rvCheckForUpdatesAction"), Qt::FindDirectChildrenOnly))
            return;
        static QPointer<RvUpdateChecker> checker;
        if (!checker)
            checker = new RvUpdateChecker(qApp);
        checker->watchWindow(dialogParent);
        const Updates::LocalVersion local = Updates::currentBuild();
        qApp->setProperty("rvLocalizedVersion", local.display);
        qApp->setProperty("rvLocalizedReleaseTag", QStringLiteral(RV_LOCALIZED_RELEASE_TAG));
        menu->addSeparator();
        QAction* action = new QAction(RvUpdateChecker::tr("Check for Updates..."), menu);
        action->setObjectName(QStringLiteral("rvCheckForUpdatesAction"));
        action->setMenuRole(QAction::NoRole);
        QPointer<QWidget> safeParent(dialogParent);
        QObject::connect(action, &QAction::triggered, checker, [safeParent]() {
            if (checker)
                checker->showDialog(safeParent.data());
        });
        menu->addAction(action);
    }
}
