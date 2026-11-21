// SPDX-License-Identifier: Apache-2.0
#include <RvCommon/RvUpdateChecker.h>
#include <RvLocalizedBuild.h>

#include <QAction>
#include <QApplication>
#include <QDesktopServices>
#include <QDateTime>
#include <QDialog>
#include <QDialogButtonBox>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonParseError>
#include <QLabel>
#include <QMenu>
#include <QMap>
#include <QSet>
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
        : QObject(parent), m_manager(manager), m_timer(new QTimer(this)), m_timeoutMs(qBound(1, timeoutMs, 15000))
    {
        setObjectName(QStringLiteral("rvUpdateChecker"));
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
        abortRequest();
        QDialog* dialog = new QDialog(parent);
        m_dialog = dialog;
        dialog->setAttribute(Qt::WA_DeleteOnClose);
        dialog->setObjectName(QStringLiteral("rvUpdateDialog"));
        dialog->setWindowTitle(tr("Check for Updates"));
        dialog->setMinimumWidth(430);
        const Updates::LocalVersion local = Updates::currentBuild();
        dialog->setProperty("rvCurrentVersion", local.display);
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
        connect(dialog, &QDialog::finished, this, [this, dialog](int) {
            if (m_dialog == dialog)
            {
                abortRequest();
                m_dialog.clear();
            }
        });
        // A document closing also destroys its child dialog; cancellation must not
        // leave a request alive or let its result reach a later dialog instance.
        connect(dialog, &QObject::destroyed, this, [this, dialog]() {
            if (m_dialog.data() == dialog || m_dialog.isNull())
                abortRequest();
        });
        dialog->show();
        if (!m_manager)
            m_manager = new QNetworkAccessManager(this);
        m_response.clear();
        m_timedOut = m_tooLarge = false;
        m_downloadUrl.clear();
        m_releaseUrl.clear();
        QNetworkRequest request(QUrl(QStringLiteral("https://api.github.com/repos/EternalSunLhx/OpenRV-Publish/releases/latest")));
        request.setAttribute(QNetworkRequest::RedirectPolicyAttribute, QNetworkRequest::ManualRedirectPolicy);
        request.setAttribute(QNetworkRequest::CacheLoadControlAttribute, QNetworkRequest::AlwaysNetwork);
        request.setRawHeader("Accept", "application/vnd.github+json");
        request.setRawHeader("Accept-Encoding", "identity");
        request.setRawHeader("X-GitHub-Api-Version", "2026-03-10");
        request.setRawHeader("User-Agent", "OpenRV-Publish-update-check/" + local.display.toUtf8());
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
        m_close->setText(tr("Close"));
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
        if (!m_dialog)
            return;
        if (m_timedOut)
            return setResult(QStringLiteral("error"), tr("The update check timed out. Try again later."));
        if (m_tooLarge || m_response.size() > maxResponseBytes)
            return setResult(QStringLiteral("error"), tr("The release response was too large."));
        const int status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
        if (status == 404)
            return setResult(QStringLiteral("no-release"), tr("This repository has no published release yet."));
        if (status == 403 || status == 429)
            return setResult(QStringLiteral("error"), tr("GitHub rejected the request or its rate limit was reached. Try again later."));
        if (reply->error() != QNetworkReply::NoError)
            return setResult(QStringLiteral("error"), tr("The update check failed. Check your Internet connection and try again."));
        if (status != 200)
            return setResult(QStringLiteral("error"), tr("The update server returned an unexpected response (HTTP %1).").arg(status));
        const Updates::ParseResult parsed = Updates::parseRelease(m_response, Updates::compiledPlatform(), Updates::compiledArchitecture());
        if (!parsed.isValid())
            return setResult(QStringLiteral("error"), tr("The release information could not be verified. No download was opened."));
        const Updates::ReleaseInfo& release = parsed.release;
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
        const Updates::Offer offer = Updates::compareRelease(Updates::currentBuild(), release.version);
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
