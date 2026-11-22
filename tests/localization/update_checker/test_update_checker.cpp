// SPDX-License-Identifier: Apache-2.0
#include <RvCommon/RvUpdateChecker.h>
#include <RvLocalizedBuild.h>
#include <QtTest/QtTest>
#include <QApplication>
#include <QCryptographicHash>
#include <QDateTime>
#include <QDialog>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLabel>
#include <QMenu>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QPushButton>
#include <QSettings>
#include <QTemporaryDir>
#include <QTimer>
#include <cstring>

using namespace Rv::Updates;

static QJsonObject releaseObject(const QString& tag = QStringLiteral("v4.2.0-zh.1"))
{
    const QString root = QStringLiteral("https://github.com/EternalSunLhx/OpenRV-Publish/releases/");
    QJsonArray assets;
    for (const QString& suffix : {QStringLiteral("Windows-x64-Setup.exe"), QStringLiteral("macOS-arm64.dmg"),
                                  QStringLiteral("macOS-x86_64.dmg"), QStringLiteral("Windows-x64-Portable.zip")})
    {
        const QString name = QStringLiteral("OpenRV-") + tag.mid(1) + '-' + suffix;
        assets.append(QJsonObject{{"name", name}, {"state", "uploaded"}, {"size", 200000000},
                                  {"browser_download_url", root + "download/" + tag + '/' + name}});
    }
    return {{"tag_name", tag}, {"draft", false}, {"prerelease", false}, {"published_at", "2026-11-18T08:00:00Z"},
            {"html_url", root + "tag/" + tag}, {"assets", assets}};
}

static void appendOfficialBuild(QJsonObject& object, quint64 build)
{
    const QString tag = object["tag_name"].toString();
    const QString root = QStringLiteral("https://github.com/EternalSunLhx/OpenRV-Publish/releases/download/") + tag + '/';
    QJsonArray assets = object["assets"].toArray();
    for (const QString& suffix : {QStringLiteral("Windows-x64-Setup.exe"), QStringLiteral("Windows-x64-Portable.zip"),
                                  QStringLiteral("Windows-x64.build.json"), QStringLiteral("Windows-x64.sha256.txt"),
                                  QStringLiteral("macOS-arm64.dmg"), QStringLiteral("macOS-arm64.zip"),
                                  QStringLiteral("macOS-arm64.build.json"), QStringLiteral("macOS-arm64.sha256.txt"),
                                  QStringLiteral("macOS-x86_64.dmg"), QStringLiteral("macOS-x86_64.zip"),
                                  QStringLiteral("macOS-x86_64.build.json"), QStringLiteral("macOS-x86_64.sha256.txt")})
    {
        const QString name = QStringLiteral("OpenRV-") + tag.mid(1) + QStringLiteral("-build.%1-").arg(build) + suffix;
        assets.append(QJsonObject{{"name", name}, {"state", "uploaded"}, {"size", 200000000},
                                  {"browser_download_url", root + name}});
    }
    object["assets"] = assets;
}

static QJsonObject officialReleaseObject(const QString& tag = QStringLiteral("v4.0.2"), quint64 build = 17)
{
    auto object = releaseObject(tag);
    object["assets"] = QJsonArray{};
    appendOfficialBuild(object, build);
    return object;
}

static void setPublicationMarker(QJsonObject& object, const QJsonObject& marker)
{
    object["body"] = QStringLiteral("Downloads and installation instructions.\n\n<!-- openrv-publication: ")
        + QString::fromUtf8(QJsonDocument(marker).toJson(QJsonDocument::Compact)) + QStringLiteral(" -->\n");
}

static QJsonObject publicationMarkerObject(const QJsonObject& object)
{
    const QString body = object["body"].toString();
    const QString prefix = QStringLiteral("<!-- openrv-publication: ");
    const int start = body.indexOf(prefix) + prefix.size();
    return QJsonDocument::fromJson(body.mid(start, body.indexOf(QStringLiteral(" -->"), start) - start).toUtf8()).object();
}

static QJsonObject cleanReleaseObject(const QString& tag = QStringLiteral("v4.0.2"), quint64 build = 35)
{
    auto object = releaseObject(tag);
    const QString prefix = QStringLiteral("OpenRV-") + tag.mid(1);
    const QString url = QStringLiteral("https://github.com/EternalSunLhx/OpenRV-Publish/releases/download/") + tag + '/';
    QJsonArray assets;
    QJsonObject hashes;
    for (const QString& suffix : {QStringLiteral("-Windows-x64-Setup.exe"), QStringLiteral("-Windows-x64-Portable.zip"),
                                  QStringLiteral("-Windows-x64.sha256.txt"), QStringLiteral("-macOS-arm64.dmg"),
                                  QStringLiteral("-macOS-arm64.zip"), QStringLiteral("-macOS-arm64.sha256.txt"),
                                  QStringLiteral("-macOS-x86_64.dmg"), QStringLiteral("-macOS-x86_64.zip"),
                                  QStringLiteral("-macOS-x86_64.sha256.txt"), QStringLiteral("-source.provenance.json"),
                                  QStringLiteral("-generated-localization.patch.zip"), QStringLiteral("-source.sha256.txt")})
    {
        const QString name = prefix + suffix;
        const QString hash = QStringLiteral("sha256:") + QString::fromLatin1(QCryptographicHash::hash(name.toUtf8(), QCryptographicHash::Sha256).toHex());
        hashes[name] = hash;
        assets.append(QJsonObject{{"name", name}, {"state", "uploaded"}, {"size", 12345},
                                  {"digest", hash}, {"browser_download_url", url + name}});
    }
    object["assets"] = assets;
    setPublicationMarker(object, {{"schema", 1}, {"tag", tag}, {"buildNumber", QString::number(build)}, {"assets", hashes}});
    return object;
}

static ReleaseVersion officialVersion(const QString& tag, quint64 build)
{
    auto version = parseReleaseVersion(tag);
    version.buildNumber = build;
    return version;
}

static QByteArray bytes(const QJsonObject& object) { return QJsonDocument(object).toJson(QJsonDocument::Compact); }

class FakeManager;
class FakeReply : public QNetworkReply
{
public:
    FakeReply(const QNetworkRequest& request, FakeManager* manager);
    void abort() override;
    void complete();
    qint64 bytesAvailable() const override { return m_data.size() - m_offset + QNetworkReply::bytesAvailable(); }
protected:
    qint64 readData(char* destination, qint64 maximum) override
    {
        const qint64 count = qMin(maximum, qint64(m_data.size()) - m_offset);
        if (count <= 0)
            return -1;
        std::memcpy(destination, m_data.constData() + m_offset, size_t(count));
        m_offset += count;
        return count;
    }
private:
    FakeManager* m_manager;
    QByteArray m_data;
    qint64 m_offset = 0;
    bool m_done = false;
};

class FakeManager : public QNetworkAccessManager
{
public:
    QByteArray payload = bytes(releaseObject());
    int status = 200;
    QNetworkReply::NetworkError responseError = QNetworkReply::NoError;
    int delay = 1;
    int requests = 0;
    int aborts = 0;
    QPointer<FakeReply> lastReply;
    QNetworkRequest lastRequest;
protected:
    QNetworkReply* createRequest(Operation operation, const QNetworkRequest& request, QIODevice*) override
    {
        Q_ASSERT(operation == GetOperation);
        ++requests;
        lastRequest = request;
        lastReply = new FakeReply(request, this);
        return lastReply;
    }
};

FakeReply::FakeReply(const QNetworkRequest& request, FakeManager* manager)
    : QNetworkReply(manager), m_manager(manager), m_data(manager->payload)
{
    setRequest(request);
    setUrl(request.url());
    setOperation(QNetworkAccessManager::GetOperation);
    setAttribute(QNetworkRequest::HttpStatusCodeAttribute, manager->status);
    open(QIODevice::ReadOnly);
    if (manager->delay >= 0)
        QTimer::singleShot(manager->delay, this, &FakeReply::complete);
}

void FakeReply::complete()
{
    if (m_done)
        return;
    m_done = true;
    if (m_manager->responseError != QNetworkReply::NoError)
        setError(m_manager->responseError, QStringLiteral("Fixture network error"));
    emit readyRead();
    setFinished(true);
    emit finished();
}

void FakeReply::abort()
{
    if (m_done)
        return;
    m_done = true;
    ++m_manager->aborts;
    setError(OperationCanceledError, QStringLiteral("Fixture cancelled"));
    setFinished(true);
    emit finished();
}

static QDialog* updateDialog()
{
    for (QWidget* widget : QApplication::topLevelWidgets())
        if (widget->objectName() == QStringLiteral("rvUpdateDialog"))
            return qobject_cast<QDialog*>(widget);
    return nullptr;
}

class DailyTestChecker : public Rv::RvUpdateChecker
{
public:
    using RvUpdateChecker::RvUpdateChecker;
    QDateTime now{QDate(2026, 11, 22), QTime(12, 0)};

protected:
    QDateTime currentDateTime() const override { return now; }
};

class UpdateCheckerTests : public QObject
{
    Q_OBJECT
private slots:
    void init()
    {
        QSettings settings(QSettings::IniFormat, QSettings::UserScope, "OpenRV-Publish", "UpdateChecker");
        settings.clear();
        settings.sync();
        QCOMPARE(settings.status(), QSettings::NoError);
    }

    void cleanup()
    {
        for (QWidget* widget : QApplication::topLevelWidgets())
            if (widget->objectName() == QStringLiteral("rvUpdateDialog"))
                widget->close();
        QCoreApplication::sendPostedEvents(nullptr, QEvent::DeferredDelete);
    }

    void strictVersions()
    {
        for (const QString& valid : {QStringLiteral("v4.1.0-zh.1"), QStringLiteral("v0.0.0-zh.42"), QStringLiteral("v65535.1.0-zh.100"), QStringLiteral("v4.0.2"), QStringLiteral("v0.0.0")})
            QVERIFY(parseReleaseVersion(valid).isValid());
        for (const QString& invalid : {QStringLiteral("4.1.0-zh.1"), QStringLiteral("v4.1.0-build.1"), QStringLiteral("v4.1.0-zh.0"),
                                      QStringLiteral("v04.1.0-zh.1"), QStringLiteral("v4.1.0-zh.01"), QStringLiteral("v4.1.0-zh.2-extra"),
                                      QStringLiteral("v65536.0.0-zh.1"), QStringLiteral("v4.1.0-zh.18446744073709551616"),
                                      QStringLiteral("v4.1.0-zh.1\n")})
            QVERIFY2(!parseReleaseVersion(invalid).isValid(), qPrintable(invalid));
    }

    void editionsAndDowngrades()
    {
        LocalVersion stable{QVersionNumber(4, 1, 0), 2, false, QStringLiteral("4.1.0-zh.2")};
        QCOMPARE(compareRelease(stable, parseReleaseVersion("v4.1.0-zh.2")), Offer::Current);
        QCOMPARE(compareRelease(stable, parseReleaseVersion("v4.1.0-zh.3")), Offer::Newer);
        QCOMPARE(compareRelease(stable, parseReleaseVersion("v4.1.0-zh.1")), Offer::Older);
        QCOMPARE(compareRelease(stable, parseReleaseVersion("v4.0.9-zh.99")), Offer::Older);
        QCOMPARE(compareRelease(stable, parseReleaseVersion("v4.2.0-zh.1")), Offer::Newer);
        LocalVersion dev{QVersionNumber(4, 2, 0), 0, true, QStringLiteral("4.2.0-dev.0123abcd")};
        QCOMPARE(compareRelease(dev, parseReleaseVersion("v4.1.0-zh.99")), Offer::Older);
        QCOMPARE(compareRelease(dev, parseReleaseVersion("v4.2.0-zh.1")), Offer::PublishedForDevelopment);
        QCOMPARE(compareRelease(dev, parseReleaseVersion("v4.3.0-zh.1")), Offer::Newer);
    }

    void officialBuildComparisonsAndLegacyMigration()
    {
        LocalVersion stable{QVersionNumber(4, 0, 2), 0, false, QStringLiteral("4.0.2+build.17"), 17, true};
        QCOMPARE(compareRelease(stable, officialVersion("v4.0.2", 17)), Offer::Current);
        QCOMPARE(compareRelease(stable, officialVersion("v4.0.2", 18)), Offer::Newer);
        QCOMPARE(compareRelease(stable, officialVersion("v4.0.2", 16)), Offer::Older);
        QCOMPARE(compareRelease(stable, officialVersion("v4.0.1", 999)), Offer::Older);
        QCOMPARE(compareRelease(stable, officialVersion("v4.0.3", 1)), Offer::Newer);
        QCOMPARE(compareRelease(stable, parseReleaseVersion("v4.0.2-zh.999")), Offer::Older);
        LocalVersion legacy{QVersionNumber(4, 0, 2), 999, false, QStringLiteral("4.0.2-zh.999")};
        QCOMPARE(compareRelease(legacy, officialVersion("v4.0.2", 1)), Offer::Newer);
        LocalVersion oldDevelopment{QVersionNumber(4, 1, 0), 0, true, QStringLiteral("4.1.0-dev.002fd32f")};
        QCOMPARE(compareRelease(oldDevelopment, officialVersion("v4.0.2", 999)), Offer::Older);
        QCOMPARE(compareRelease(oldDevelopment, officialVersion("v4.1.0", 1)), Offer::PublishedForDevelopment);
        QCOMPARE(officialVersion("v4.0.2", 17).displayVersion(), QStringLiteral("4.0.2+build.17"));
    }

    void cleanPublicationAssetsKeepInternalBuildAndAllPlatforms()
    {
        const auto object = cleanReleaseObject();
        for (const auto& target : {qMakePair(QString("Windows"), QString("x64")),
                                    qMakePair(QString("macOS"), QString("arm64")),
                                    qMakePair(QString("macOS"), QString("x86_64"))})
        {
            const auto parsed = parseRelease(bytes(object), target.first, target.second);
            QVERIFY(parsed.isValid());
            QCOMPARE(parsed.release.version.buildNumber, quint64(35));
            QVERIFY(!parsed.release.downloadUrl.isEmpty());
            QVERIFY(!parsed.release.downloadUrl.fileName().contains("-build."));
            const LocalVersion local{QVersionNumber(4, 0, 2), 0, false, QStringLiteral("4.0.2+build.34"), 34, true};
            QCOMPARE(compareRelease(local, parsed.release.version), Offer::Newer);
        }
        QVERIFY(parseRelease(bytes(object), "Windows", "arm64").release.downloadUrl.isEmpty());
        const auto maximum = parseRelease(bytes(cleanReleaseObject("v4.0.2", 18446744073709551615ULL)), "Windows", "x64");
        QVERIFY(maximum.isValid());
        QCOMPARE(maximum.release.version.buildNumber, 18446744073709551615ULL);
    }

    void cleanPublicationRequiresOneStrictMarkerWithoutFallback()
    {
        const auto correct = cleanReleaseObject();
        const QString body = correct["body"].toString();
        QStringList malformed{body + body, QStringLiteral("<!-- openrv-publication: bad json -->"),
                              QString(body).replace("openrv-publication: ", "openrv-publication:"),
                              QString(body).replace("openrv-publication", "OPENRV-PUBLICATION"),
                              QString(body).replace("\"buildNumber\":\"35\"", "\"buildNumber\":\"35\",\"buildNumber\":\"36\""),
                              QString(body).replace("\"buildNumber\":\"35\"", "\"buildNumber\":\"35\",\"build\\u004eumber\":\"36\"")};
        const QString name = correct["assets"].toArray()[0].toObject()["name"].toString();
        const QString hash = correct["assets"].toArray()[0].toObject()["digest"].toString();
        const QString member = '"' + name + QStringLiteral("\":\"") + hash + '"';
        malformed.append(QString(body).replace(member, member + ',' + member));
        malformed.append(QStringLiteral("<!-- openrv-publication: \n")
                         + QString::fromUtf8(QJsonDocument(publicationMarkerObject(correct)).toJson()) + QStringLiteral(" -->\n"));
        for (const QString& bad : malformed)
        {
            auto object = correct;
            object["body"] = bad;
            QCOMPARE(parseRelease(bytes(object), "Windows", "x64").error, ParseError::InvalidAsset);
            appendOfficialBuild(object, 99); // Invalid marker must never select the legacy fallback.
            QCOMPARE(parseRelease(bytes(object), "Windows", "x64").error, ParseError::InvalidAsset);
        }
        auto absent = correct;
        absent.remove("body");
        const auto noMarker = parseRelease(bytes(absent), "Windows", "x64");
        QVERIFY(noMarker.isValid());
        QVERIFY(noMarker.release.downloadUrl.isEmpty());
        QCOMPARE(noMarker.release.version.buildNumber, quint64(0));
    }

    void cleanPublicationRejectsInvalidSchemaTagCountersAndHashInventory()
    {
        for (const QJsonValue& counter : {QJsonValue(35), QJsonValue(true), QJsonValue("0"), QJsonValue("035"),
                                          QJsonValue("-1"), QJsonValue("18446744073709551616")})
        {
            auto object = cleanReleaseObject();
            auto marker = publicationMarkerObject(object);
            marker["buildNumber"] = counter;
            setPublicationMarker(object, marker);
            QCOMPARE(parseRelease(bytes(object), "Windows", "x64").error, ParseError::InvalidAsset);
        }
        for (const QString& change : {QStringLiteral("schema"), QStringLiteral("tag"), QStringLiteral("extra"),
                                      QStringLiteral("missing-hash"), QStringLiteral("bad-hash"), QStringLiteral("extra-hash")})
        {
            auto object = cleanReleaseObject();
            auto marker = publicationMarkerObject(object);
            if (change == "schema") marker["schema"] = true;
            if (change == "tag") marker["tag"] = "v4.0.3";
            if (change == "extra") marker["extra"] = 1;
            auto hashes = marker["assets"].toObject();
            if (change == "missing-hash") hashes.remove(hashes.keys().first());
            if (change == "bad-hash") hashes[hashes.keys().first()] = QStringLiteral("sha256:") + QString(64, 'G');
            if (change == "extra-hash") hashes["OpenRV-4.0.2-Windows-x64.build.json"] = QStringLiteral("sha256:") + QString(64, '0');
            marker["assets"] = hashes;
            setPublicationMarker(object, marker);
            QCOMPARE(parseRelease(bytes(object), "Windows", "x64").error, ParseError::InvalidAsset);
        }
    }

    void cleanPublicationRejectsIncompleteDuplicateMixedOrUnboundAssets()
    {
        for (const QString& change : {QStringLiteral("missing"), QStringLiteral("duplicate"), QStringLiteral("legacy-mix"),
                                      QStringLiteral("digest"), QStringLiteral("digest-missing"), QStringLiteral("url"),
                                      QStringLiteral("state"), QStringLiteral("size")})
        {
            auto object = cleanReleaseObject();
            auto assets = object["assets"].toArray();
            auto asset = assets[0].toObject();
            if (change == "missing") assets.removeLast();
            if (change == "duplicate") assets[1] = asset;
            if (change == "digest") asset["digest"] = QStringLiteral("sha256:") + QString(64, '0');
            if (change == "digest-missing") asset.remove("digest");
            if (change == "url") asset["browser_download_url"] = "https://foreign.invalid/file";
            if (change == "state") asset["state"] = "new";
            if (change == "size") asset["size"] = 0;
            assets[0] = asset;
            object["assets"] = assets;
            if (change == "legacy-mix") appendOfficialBuild(object, 36);
            QCOMPARE(parseRelease(bytes(object), "Windows", "x64").error, ParseError::InvalidAsset);
        }
    }

    void cleanPublicationUpgradeDialogUsesTheCleanInstallerUrl()
    {
        const auto local = currentBuild();
        const quint64 build = local.officialTag ? local.buildNumber + 1 : 35;
        FakeManager manager;
        manager.payload = bytes(cleanReleaseObject('v' + local.base.toString(), build));
        Rv::RvUpdateChecker checker(nullptr, &manager);
        checker.showDialog();
        QTRY_VERIFY(updateDialog()->property("rvUpdateResult") != QStringLiteral("checking"));
        QCOMPARE(updateDialog()->property("rvLatestBuildNumber").toULongLong(), build);
        if (compiledPlatform() != "unsupported")
        {
            const QString expected = local.development ? "development-alternative" : "newer";
            QCOMPARE(updateDialog()->property("rvUpdateResult").toString(), expected);
            QVERIFY(!updateDialog()->property("rvDownloadUrl").toUrl().fileName().contains("-build."));
            QVERIFY(!updateDialog()->property("rvDownloadUrl").toUrl().isEmpty());
        }
    }

    void officialPlatformAssets()
    {
        const auto object = officialReleaseObject();
        for (const auto& target : {qMakePair(QString("Windows"), QString("x64")),
                                  qMakePair(QString("macOS"), QString("arm64")),
                                  qMakePair(QString("macOS"), QString("x86_64"))})
        {
            const auto result = parseRelease(bytes(object), target.first, target.second);
            QVERIFY(result.isValid());
            QCOMPARE(result.release.version.buildNumber, quint64(17));
            QVERIFY(result.release.version.officialTag);
            const QString suffix = target.first == "Windows" ? "-Windows-x64-Setup.exe" : "-macOS-" + target.second + ".dmg";
            QCOMPARE(result.release.downloadUrl.fileName(), QString("OpenRV-4.0.2-build.17") + suffix);
            QVERIFY(result.release.downloadUrl.toString().contains("/download/v4.0.2/"));
        }
        const auto unsupported = parseRelease(bytes(object), "Windows", "arm64");
        QVERIFY(unsupported.isValid());
        QVERIFY(unsupported.release.downloadUrl.isEmpty());
    }

    void highestCompleteOfficialBuild()
    {
        auto object = officialReleaseObject();
        appendOfficialBuild(object, 18);
        appendOfficialBuild(object, 19);
        auto assets = object["assets"].toArray();
        for (int index = 0; index < assets.size(); ++index)
        {
            auto asset = assets[index].toObject();
            if (asset["name"].toString() == "OpenRV-4.0.2-build.19-Windows-x64-Portable.zip")
            {
                asset["state"] = "new";
                assets[index] = asset;
            }
        }
        object["assets"] = assets;
        const auto windows = parseRelease(bytes(object), "Windows", "x64");
        QVERIFY(windows.isValid());
        QCOMPARE(windows.release.version.buildNumber, quint64(18));
        QCOMPARE(parseRelease(bytes(object), "macOS", "arm64").release.version.buildNumber, quint64(19));
        // A completed setup alone cannot borrow companions from build 18.
        for (int index = 0; index < assets.size(); ++index)
        {
            auto asset = assets[index].toObject();
            if (asset["name"].toString() == "OpenRV-4.0.2-build.18-Windows-x64.build.json")
            {
                asset["size"] = 0;
                assets[index] = asset;
            }
        }
        object["assets"] = assets;
        QCOMPARE(parseRelease(bytes(object), "Windows", "x64").release.version.buildNumber, quint64(17));
    }

    void officialBuildAssetsStayBoundToTagAndRepository()
    {
        auto object = officialReleaseObject();
        auto assets = object["assets"].toArray();
        auto asset = assets[0].toObject();
        asset["browser_download_url"] = "https://github.com/EternalSunLhx/OpenRV-Publish/releases/download/v4.0.3/" + asset["name"].toString();
        assets[0] = asset; object["assets"] = assets;
        QCOMPARE(parseRelease(bytes(object), "Windows", "x64").error, ParseError::InvalidAsset);
        object = officialReleaseObject(); assets = object["assets"].toArray();
        asset = assets[2].toObject(); asset["browser_download_url"] = "https://example.invalid/manifest.json";
        assets[2] = asset; object["assets"] = assets;
        QCOMPARE(parseRelease(bytes(object), "Windows", "x64").error, ParseError::InvalidAsset);
        object = officialReleaseObject(); assets = object["assets"].toArray(); assets.append(assets[0]); object["assets"] = assets;
        QCOMPARE(parseRelease(bytes(object), "Windows", "x64").error, ParseError::InvalidAsset);
        object = officialReleaseObject("v4.0.3"); object["tag_name"] = "v4.0.2";
        object["html_url"] = "https://github.com/EternalSunLhx/OpenRV-Publish/releases/tag/v4.0.2";
        const auto wrongVersion = parseRelease(bytes(object), "Windows", "x64");
        QVERIFY(wrongVersion.isValid());
        QVERIFY(wrongVersion.release.downloadUrl.isEmpty());
    }

    void officialBuildCountersAndAccumulatedAssets()
    {
        auto object = officialReleaseObject("v4.0.2", 18446744073709551615ULL);
        const auto maximum = parseRelease(bytes(object), "Windows", "x64");
        QVERIFY(maximum.isValid());
        QCOMPARE(maximum.release.version.buildNumber, 18446744073709551615ULL);
        object = officialReleaseObject();
        auto assets = object["assets"].toArray();
        auto asset = assets[0].toObject();
        asset["name"] = "OpenRV-4.0.2-build.18446744073709551616-Windows-x64-Setup.exe";
        assets[0] = asset; object["assets"] = assets;
        QCOMPARE(parseRelease(bytes(object), "Windows", "x64").error, ParseError::InvalidAsset);
        object = officialReleaseObject("v4.0.2", 1);
        for (quint64 build = 2; build <= 12; ++build)
            appendOfficialBuild(object, build);
        QVERIFY(object["assets"].toArray().size() > 128);
        QCOMPARE(parseRelease(bytes(object), "Windows", "x64").release.version.buildNumber, quint64(12));
        assets = {};
        for (int index = 0; index < 4097; ++index)
            assets.append(QJsonObject{});
        object["assets"] = assets;
        QCOMPARE(parseRelease(bytes(object), "Windows", "x64").error, ParseError::InvalidAsset);
    }

    void officialSameBaseUpgradeDialog()
    {
        const auto local = currentBuild();
        const quint64 build = local.officialTag ? local.buildNumber + 1 : 17;
        FakeManager manager;
        manager.payload = bytes(officialReleaseObject('v' + local.base.toString(), build));
        Rv::RvUpdateChecker checker(nullptr, &manager);
        checker.showDialog();
        QTRY_VERIFY(updateDialog()->property("rvUpdateResult") != QStringLiteral("checking"));
        const QString expected = compiledPlatform() == "unsupported" ? "no-package"
            : local.development ? "development-alternative" : "newer";
        QCOMPARE(updateDialog()->property("rvUpdateResult").toString(), expected);
        QCOMPARE(updateDialog()->property("rvLatestBuildNumber").toULongLong(), build);
        QCOMPARE(updateDialog()->property("rvLatestVersion").toString(), local.base.toString() + QString("+build.%1").arg(build));
    }

    void platformAssets()
    {
        const auto object = releaseObject();
        QCOMPARE(parseRelease(bytes(object), "Windows", "x64").release.downloadUrl.fileName(), QStringLiteral("OpenRV-4.2.0-zh.1-Windows-x64-Setup.exe"));
        QCOMPARE(parseRelease(bytes(object), "macOS", "arm64").release.downloadUrl.fileName(), QStringLiteral("OpenRV-4.2.0-zh.1-macOS-arm64.dmg"));
        QCOMPARE(parseRelease(bytes(object), "macOS", "x86_64").release.downloadUrl.fileName(), QStringLiteral("OpenRV-4.2.0-zh.1-macOS-x86_64.dmg"));
        const auto unsupported = parseRelease(bytes(object), "Windows", "arm64");
        QVERIFY(unsupported.isValid());
        QVERIFY(unsupported.release.downloadUrl.isEmpty());
    }

    void malformedOrUnpublishedReleases()
    {
        QVERIFY(!parseRelease("not json", "Windows", "x64").isValid());
        QVERIFY(!parseRelease("[]", "Windows", "x64").isValid());
        auto object = releaseObject();
        object["draft"] = true;
        QCOMPARE(parseRelease(bytes(object), "Windows", "x64").error, ParseError::InvalidRelease);
        object = releaseObject(); object["prerelease"] = true;
        QCOMPARE(parseRelease(bytes(object), "Windows", "x64").error, ParseError::InvalidRelease);
        object = releaseObject(); object.remove("draft");
        QCOMPARE(parseRelease(bytes(object), "Windows", "x64").error, ParseError::InvalidRelease);
        object = releaseObject(); object.remove("published_at");
        QCOMPARE(parseRelease(bytes(object), "Windows", "x64").error, ParseError::InvalidRelease);
        object = releaseObject(); object["tag_name"] = "v4.2.0-dev.0123abcd";
        QCOMPARE(parseRelease(bytes(object), "Windows", "x64").error, ParseError::InvalidVersion);
    }

    void foreignAndAmbiguousUrls()
    {
        for (const QString& bad : {QStringLiteral("http://github.com/EternalSunLhx/OpenRV-Publish/releases/tag/v4.2.0-zh.1"),
                                  QStringLiteral("https://github.com/another/repo/releases/tag/v4.2.0-zh.1"),
                                  QStringLiteral("https://github.com.evil.invalid/EternalSunLhx/OpenRV-Publish/releases/tag/v4.2.0-zh.1"),
                                  QStringLiteral("https://github.com/EternalSunLhx/OpenRV-Publish/releases/tag/v4.2.0-zh.1?redirect=bad")})
        {
            auto object = releaseObject(); object["html_url"] = bad;
            QCOMPARE(parseRelease(bytes(object), "Windows", "x64").error, ParseError::InvalidUrl);
        }
        auto object = releaseObject();
        QJsonArray assets = object["assets"].toArray();
        auto asset = assets[0].toObject();
        asset["browser_download_url"] = "https://example.invalid/update.exe"; assets[0] = asset; object["assets"] = assets;
        QCOMPARE(parseRelease(bytes(object), "Windows", "x64").error, ParseError::InvalidAsset);
        object = releaseObject(); assets = object["assets"].toArray(); assets.append(assets[0]); object["assets"] = assets;
        QCOMPARE(parseRelease(bytes(object), "Windows", "x64").error, ParseError::InvalidAsset);
        object = releaseObject(); assets = object["assets"].toArray(); asset = assets[0].toObject();
        asset["state"] = "new"; assets[0] = asset; object["assets"] = assets;
        QCOMPARE(parseRelease(bytes(object), "Windows", "x64").error, ParseError::InvalidAsset);
    }

    void compiledMetadata()
    {
        const auto local = currentBuild();
        QCOMPARE(local.base, QVersionNumber(RV_LOCALIZED_MAJOR, RV_LOCALIZED_MINOR, RV_LOCALIZED_PATCH));
        QCOMPARE(local.display, QStringLiteral(RV_LOCALIZED_VERSION));
        const auto version = parseReleaseVersion(QStringLiteral(RV_LOCALIZED_RELEASE_TAG));
        if (!version.isValid())
            QVERIFY(local.development);
        else if (version.officialTag)
        {
            QVERIFY(!local.development);
            QVERIFY(local.officialTag);
            QVERIFY(RV_LOCALIZED_BUILD_NUMBER > 0);
            QCOMPARE(local.buildNumber, quint64(RV_LOCALIZED_BUILD_NUMBER));
            QCOMPARE(local.base, version.base);
        }
        else
        {
            QVERIFY(!local.development);
            QVERIFY(!local.officialTag);
            QCOMPARE(local.edition, version.edition);
        }
    }

    void manualActionDoesNotStartNetwork()
    {
        QMenu menu;
        Rv::addUpdateCheckAction(&menu, nullptr);
        Rv::addUpdateCheckAction(&menu, nullptr);
        QCOMPARE(menu.findChildren<QAction*>(QStringLiteral("rvCheckForUpdatesAction")).size(), 1);
        auto checker = qApp->findChild<Rv::RvUpdateChecker*>(QStringLiteral("rvUpdateChecker"));
        QVERIFY(checker);
        QVERIFY(checker->findChildren<QNetworkAccessManager*>().isEmpty());
        QCOMPARE(menu.findChild<QAction*>(QStringLiteral("rvCheckForUpdatesAction"))->menuRole(), QAction::NoRole);
        QCOMPARE(qApp->property("rvLocalizedVersion").toString(), currentBuild().display);
        QVERIFY(!updateDialog());
    }

    void dailyCheckIsSharedByVisibleWindowsAndControllers()
    {
        FakeManager manager;
        manager.delay = -1;
        manager.status = 404;
        QWidget firstWindow, secondWindow;
        DailyTestChecker first(nullptr, &manager), second(nullptr, &manager);
        first.watchWindow(&firstWindow);
        first.watchWindow(&firstWindow);
        second.watchWindow(&secondWindow);
        QCoreApplication::processEvents();
        QCOMPARE(manager.requests, 0);

        firstWindow.show();
        secondWindow.show();
        first.watchWindow(&secondWindow);
        QTRY_COMPARE(manager.requests, 1);
        QVERIFY(QMetaObject::invokeMethod(&first, "checkAutomatically", Qt::DirectConnection));
        QVERIFY(QMetaObject::invokeMethod(&second, "checkAutomatically", Qt::DirectConnection));
        QCOMPARE(manager.requests, 1);
        QVERIFY(!updateDialog());
        manager.lastReply->complete();
        QVERIFY(!updateDialog());
    }

    void laterSurvivesRestartAndManualCheckRemainsAvailable()
    {
        FakeManager manager;
        manager.delay = -1;
        const auto local = currentBuild();
        const QString tag = local.officialTag ? 'v' + local.base.toString() : QStringLiteral("v5.0.0");
        const quint64 build = local.officialTag ? local.buildNumber + 1 : 35;
        manager.payload = bytes(cleanReleaseObject(tag, build));
        const QUrl installer = parseRelease(manager.payload, compiledPlatform(), compiledArchitecture()).release.downloadUrl;
        if (installer.isEmpty())
            QSKIP("The automatic offer requires a supported installer.");

        QWidget window;
        window.show();
        {
            DailyTestChecker checker(nullptr, &manager);
            checker.watchWindow(&window);
            QTRY_COMPARE(manager.requests, 1);
            QVERIFY(!updateDialog());
            manager.lastReply->complete();
            QTRY_VERIFY(updateDialog() && updateDialog()->isVisible());
            QVERIFY(updateDialog()->property("rvAutomaticUpdate").toBool());
            QCOMPARE(updateDialog()->property("rvUpdateResult").toString(), QStringLiteral("newer"));
            QCOMPARE(updateDialog()->property("rvLatestReleaseTag").toString(), tag);
            QCOMPARE(updateDialog()->property("rvLatestBuildNumber").toULongLong(), build);
            auto latest = updateDialog()->findChild<QLabel*>("rvLatestUpdateVersion");
            QVERIFY(latest);
            const QString version = tag.mid(1) + QStringLiteral("+build.%1").arg(build);
            QCOMPARE(latest->text(), QStringLiteral("Latest published version: %1").arg(version));
            QCOMPARE(updateDialog()->property("rvDownloadUrl").toUrl(), installer);
            auto download = updateDialog()->findChild<QPushButton*>("rvUpdateDownloadButton");
            auto later = updateDialog()->findChild<QPushButton*>("rvUpdateCloseButton");
            QVERIFY(download && download->isVisible());
            QVERIFY(later && later->isVisible());
            QCOMPARE(later->text(), QStringLiteral("Later"));
            later->click();
            QTRY_VERIFY(!updateDialog());
        }
        {
            DailyTestChecker restarted(nullptr, &manager);
            restarted.watchWindow(&window);
            QCoreApplication::processEvents();
            QVERIFY(QMetaObject::invokeMethod(&restarted, "checkAutomatically", Qt::DirectConnection));
            QCOMPARE(manager.requests, 1);
            QVERIFY(!updateDialog());

            restarted.showDialog(&window);
            QCOMPARE(manager.requests, 2);
            manager.lastReply->complete();
            QTRY_VERIFY(updateDialog() && updateDialog()->property("rvUpdateResult").toString() == "newer");
            QVERIFY(!updateDialog()->property("rvAutomaticUpdate").toBool());
            updateDialog()->reject();
            QTRY_VERIFY(!updateDialog());

            restarted.now = restarted.now.addDays(1);
            QVERIFY(QMetaObject::invokeMethod(&restarted, "checkAutomatically", Qt::DirectConnection));
            QCOMPARE(manager.requests, 3);
            manager.lastReply->complete();
            QTRY_VERIFY(updateDialog() && updateDialog()->property("rvAutomaticUpdate").toBool());

            // The user resumes the next morning and dismisses yesterday's
            // still-open reminder before the next automatic check runs.
            restarted.now = restarted.now.addDays(1);
            auto later = updateDialog()->findChild<QPushButton*>("rvUpdateCloseButton");
            QVERIFY(later);
            later->click();
            QTRY_VERIFY(!updateDialog());
            QVERIFY(QMetaObject::invokeMethod(&restarted, "checkAutomatically", Qt::DirectConnection));
            QCOMPARE(manager.requests, 3);
        }
    }

    void openWindowChecksAgainAtLocalMidnight()
    {
        FakeManager manager;
        manager.delay = -1;
        manager.status = 404;
        QWidget window;
        DailyTestChecker checker(nullptr, &manager);
        checker.now = QDateTime(QDate(2026, 12, 31), QTime(23, 59, 59, 900));
        window.show();
        checker.watchWindow(&window);
        QTRY_COMPARE(manager.requests, 1);
        manager.lastReply->complete();
        QVERIFY(!updateDialog());

        // No new window, activation, or explicit check. The production timer
        // must wake the still-open application on the new local calendar day.
        checker.now = checker.now.addMSecs(150);
        QTRY_COMPARE_WITH_TIMEOUT(manager.requests, 2, 3000);
        manager.lastReply->complete();
        QVERIFY(QMetaObject::invokeMethod(&checker, "checkAutomatically", Qt::DirectConnection));
        QCOMPARE(manager.requests, 2);
        QVERIFY(!updateDialog());
    }

    void automaticNonOffersStaySilent_data()
    {
        QTest::addColumn<int>("status");
        QTest::addColumn<QByteArray>("payload");
        QTest::addColumn<int>("networkError");
        const auto local = currentBuild();
        const QString tag = 'v' + local.base.toString();
        auto same = local.officialTag ? cleanReleaseObject(tag, local.buildNumber)
            : releaseObject(tag + QStringLiteral("-zh.%1").arg(local.development ? 1 : local.edition));
        auto missing = officialReleaseObject("v5.0.0");
        missing["assets"] = QJsonArray{};
        const int noError = int(QNetworkReply::NoError);
        QTest::newRow("current-or-development-alternative") << 200 << bytes(same) << noError;
        QTest::newRow("older") << 200 << bytes(cleanReleaseObject("v1.0.0")) << noError;
        QTest::newRow("no-installer") << 200 << bytes(missing) << noError;
        QTest::newRow("no-release") << 404 << QByteArray("{}") << noError;
        QTest::newRow("offline") << 0 << QByteArray() << int(QNetworkReply::HostNotFoundError);
        QTest::newRow("bad-metadata") << 200 << QByteArray("invalid") << noError;
    }

    void yesterdayRequestStillRunningAtMidnightDoesNotConsumeToday()
    {
        FakeManager manager;
        manager.delay = -1;
        manager.status = 404;
        QWidget window;
        DailyTestChecker checker(nullptr, &manager);
        checker.now = QDateTime(QDate(2026, 11, 22), QTime(23, 59, 58));
        window.show();
        checker.watchWindow(&window);
        QTRY_COMPARE(manager.requests, 1);
        checker.now = checker.now.addSecs(3);
        QVERIFY(QMetaObject::invokeMethod(&checker, "checkAutomatically", Qt::DirectConnection));
        QCOMPARE(manager.requests, 1);
        manager.lastReply->complete();
        QTRY_COMPARE(manager.requests, 2);
        manager.lastReply->complete();
        QVERIFY(!updateDialog());
    }

    void automaticNonOffersStaySilent()
    {
        QFETCH(int, status);
        QFETCH(QByteArray, payload);
        QFETCH(int, networkError);
        FakeManager manager;
        manager.delay = -1;
        manager.status = status;
        manager.payload = payload;
        manager.responseError = static_cast<QNetworkReply::NetworkError>(networkError);
        QWidget window;
        DailyTestChecker checker(nullptr, &manager);
        window.show();
        checker.watchWindow(&window);
        QTRY_COMPARE(manager.requests, 1);
        QVERIFY(!updateDialog());
        manager.lastReply->complete();
        QCoreApplication::processEvents();
        QVERIFY(!updateDialog());
        QVERIFY(QMetaObject::invokeMethod(&checker, "checkAutomatically", Qt::DirectConnection));
        QCOMPARE(manager.requests, 1);
    }

    void manualDialogAdoptsAnInFlightDailyCheck()
    {
        FakeManager manager;
        manager.delay = -1;
        manager.status = 403;
        QWidget window;
        DailyTestChecker checker(nullptr, &manager);
        window.show();
        checker.watchWindow(&window);
        QTRY_COMPARE(manager.requests, 1);
        QVERIFY(!updateDialog());

        checker.showDialog(&window);
        QVERIFY(updateDialog() && updateDialog()->isVisible());
        QCOMPARE(updateDialog()->property("rvUpdateResult").toString(), QStringLiteral("checking"));
        QCOMPARE(manager.requests, 1);
        QCOMPARE(manager.aborts, 0);
        manager.lastReply->complete();
        QTRY_COMPARE(updateDialog()->property("rvUpdateResult").toString(), QStringLiteral("error"));
        QVERIFY(!updateDialog()->property("rvAutomaticUpdate").toBool());
        QCOMPARE(manager.requests, 1);
    }

    void lastDocumentClosedBeforeDailyReplyCannotLeaveAPrompt()
    {
        FakeManager manager;
        manager.delay = -1;
        manager.payload = bytes(cleanReleaseObject("v5.0.0"));
        DailyTestChecker checker(nullptr, &manager);
        {
            QWidget window;
            checker.watchWindow(&window);
            window.show();
            QTRY_COMPARE(manager.requests, 1);
        }
        // Implementations may also cancel when the final document disappears.
        if (manager.lastReply && !manager.lastReply->isFinished())
            manager.lastReply->complete();
        QCoreApplication::processEvents();
        QVERIFY(!updateDialog());
    }

    void successfulRequestAndHeaders()
    {
        FakeManager manager;
        Rv::RvUpdateChecker checker(nullptr, &manager);
        checker.showDialog();
        QVERIFY(updateDialog());
        QTRY_VERIFY(updateDialog()->property("rvUpdateResult") != QStringLiteral("checking"));
        const QString expected = compiledPlatform() == "unsupported" ? "no-package" : "newer";
        QCOMPARE(updateDialog()->property("rvUpdateResult").toString(), expected);
        QCOMPARE(manager.requests, 1);
        QCOMPARE(manager.lastRequest.url().toString(), QStringLiteral("https://api.github.com/repos/EternalSunLhx/OpenRV-Publish/releases/latest"));
        QVERIFY(!manager.lastRequest.hasRawHeader("Authorization"));
        QCOMPARE(manager.lastRequest.attribute(QNetworkRequest::RedirectPolicyAttribute).toInt(), int(QNetworkRequest::ManualRedirectPolicy));
        QCOMPARE(updateDialog()->property("rvLatestVersion").toString(), QStringLiteral("v4.2.0-zh.1"));
        QVERIFY(updateDialog()->findChild<QPushButton*>(QStringLiteral("rvUpdateReleaseNotesButton"))->isVisible());
    }

    void developmentOrSameEdition()
    {
        const auto local = currentBuild();
        FakeManager manager;
        if (local.officialTag)
            manager.payload = bytes(officialReleaseObject('v' + local.base.toString(), local.buildNumber));
        else
            manager.payload = bytes(releaseObject('v' + local.base.toString()
                + QStringLiteral("-zh.%1").arg(local.development ? 2 : local.edition)));
        Rv::RvUpdateChecker checker(nullptr, &manager);
        checker.showDialog();
        QTRY_VERIFY(updateDialog()->property("rvUpdateResult") != QStringLiteral("checking"));
        const QString expected = !local.development ? "current" : compiledPlatform() == "unsupported" ? "no-package" : "development-alternative";
        QCOMPARE(updateDialog()->property("rvUpdateResult").toString(), expected);
    }

    void httpFailures_data()
    {
        QTest::addColumn<int>("status"); QTest::addColumn<QByteArray>("payload"); QTest::addColumn<QString>("result");
        QTest::newRow("no-release") << 404 << QByteArray("{}") << QString("no-release");
        QTest::newRow("rate-limit") << 403 << QByteArray("{}") << QString("error");
        QTest::newRow("too-many-requests") << 429 << QByteArray("{}") << QString("error");
        QTest::newRow("redirect") << 301 << QByteArray("{}") << QString("error");
        QTest::newRow("invalid-json") << 200 << QByteArray("not json") << QString("error");
        QTest::newRow("response-size") << 200 << QByteArray(2 * 1024 * 1024 + 10, 'x') << QString("error");
    }
    void httpFailures()
    {
        QFETCH(int, status); QFETCH(QByteArray, payload); QFETCH(QString, result);
        FakeManager manager; manager.status = status; manager.payload = payload;
        Rv::RvUpdateChecker checker(nullptr, &manager);
        checker.showDialog();
        QTRY_VERIFY(updateDialog()->property("rvUpdateResult") != QStringLiteral("checking"));
        QCOMPARE(updateDialog()->property("rvUpdateResult").toString(), result);
        QVERIFY(updateDialog()->findChild<QPushButton*>(QStringLiteral("rvUpdateDownloadButton"))->isHidden());
    }

    void sslFailureIsNotIgnored()
    {
        FakeManager manager; manager.status = 0; manager.responseError = QNetworkReply::SslHandshakeFailedError;
        Rv::RvUpdateChecker checker(nullptr, &manager);
        checker.showDialog();
        QTRY_COMPARE(updateDialog()->property("rvUpdateResult").toString(), QString("error"));
        QVERIFY(updateDialog()->findChild<QPushButton*>(QStringLiteral("rvUpdateDownloadButton"))->isHidden());
    }

    void cancelAndRepeatedRequests()
    {
        FakeManager manager; manager.delay = 100000;
        Rv::RvUpdateChecker checker(nullptr, &manager);
        checker.showDialog();
        QDialog* first = updateDialog();
        checker.showDialog();
        QCOMPARE(updateDialog(), first);
        QCOMPARE(manager.requests, 1);
        first->reject();
        QCoreApplication::sendPostedEvents(nullptr, QEvent::DeferredDelete);
        QCOMPARE(manager.aborts, 1);
        QVERIFY(!updateDialog());
        checker.showDialog();
        QCOMPARE(manager.requests, 2);
        updateDialog()->reject();
        QCOMPARE(manager.aborts, 2);
    }

    void timeoutIsBounded()
    {
        FakeManager manager; manager.delay = 100000;
        Rv::RvUpdateChecker checker(nullptr, &manager, 10);
        checker.showDialog();
        QTRY_COMPARE(updateDialog()->property("rvUpdateResult").toString(), QString("error"));
        QCOMPARE(manager.aborts, 1);
        QVERIFY(updateDialog()->findChild<QLabel*>(QStringLiteral("rvUpdateStatus"))->text().contains("timed out"));
    }

    void closedDialogCannotCancelItsReplacement()
    {
        FakeManager manager; manager.delay = 100000;
        Rv::RvUpdateChecker checker(nullptr, &manager);
        checker.showDialog();
        updateDialog()->reject();
        checker.showDialog();
        QCOMPARE(manager.requests, 2);
        QCOMPARE(manager.aborts, 1);
        QCoreApplication::sendPostedEvents(nullptr, QEvent::DeferredDelete);
        QVERIFY(updateDialog());
        QCOMPARE(manager.aborts, 1);
        QCOMPARE(updateDialog()->property("rvUpdateResult").toString(), QStringLiteral("checking"));
    }

    void closingTheDocumentCancelsItsRequest()
    {
        FakeManager manager; manager.delay = 100000;
        Rv::RvUpdateChecker checker(nullptr, &manager);
        QWidget* document = new QWidget;
        checker.showDialog(document);
        QCOMPARE(manager.requests, 1);
        delete document;
        QCOMPARE(manager.aborts, 1);
        QVERIFY(!updateDialog());
    }
};

int main(int argc, char** argv)
{
    qputenv("QT_QPA_PLATFORM", "offscreen");
    qunsetenv("RV_DISABLE_AUTOMATIC_UPDATE_CHECK");
    QApplication application(argc, argv);
    QTemporaryDir settingsDirectory;
    if (!settingsDirectory.isValid())
        return 1;
    QSettings::setPath(QSettings::IniFormat, QSettings::UserScope, settingsDirectory.path());
    UpdateCheckerTests tests;
    return QTest::qExec(&tests, argc, argv);
}

#include "test_update_checker.moc"
