// Mechanism fixture only: this does not certify an OpenRV product build.
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <QApplication>
#include <QColor>
#include <QDir>
#include <QFileInfo>
#include <QImage>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QSurfaceFormat>
#include <QTimer>
#include <QVariant>
#include <QWebEnginePage>
#include <QWebEngineProfile>
#include <QWebEngineView>
#include <QWindow>
#include <mach-o/dyld.h>
#include <cmath>
#include <iostream>
#include <memory>

static QString pointerText(const void* value)
{
    return "0x" + QString::number(reinterpret_cast<quintptr>(value), 16);
}

static QJsonArray images()
{
    QJsonArray result;
    // Stage snapshots; no probe code unloads libraries. Keep the actual loaded
    // provider paths, independently of Objective-C warning text.
    const uint32_t count = _dyld_image_count();
    for (uint32_t i = 0; i < count; ++i)
    {
        const char* raw = _dyld_get_image_name(i);
        if (!raw) continue;
        const QString name = QString::fromUtf8(raw);
        const int marker = name.indexOf(".framework/");
        if (marker < 0) continue;
        const QString framework = QFileInfo(name.left(marker)).fileName();
        if (!framework.startsWith("Qt")) continue;
        result.append(QJsonObject{{"framework", framework}, {"loadedPath", name},
                                  {"realPath", QFileInfo(name).canonicalFilePath()}});
    }
    return result;
}

static void checkpoint(QJsonObject report, const QString& phase)
{
    report["check"] = "native-mixed-qt-python-fixture";
    report["productRuntimeVerified"] = false;
    report["phase"] = phase;
    report["loadedQtImages"] = images();
    std::cout << QJsonDocument(report).toJson(QJsonDocument::Compact).constData() << std::endl;
}

static bool pythonRun(const char* script, QJsonObject& report)
{
    if (PyRun_SimpleString(script) != 0)
    {
        PyErr_Print();
        report["pythonError"] = "Embedded Python operation failed";
        return false;
    }
    return true;
}

int main(int argc, char** argv)
{
    bool embedded = false;
    QString home, packages;
    for (int i = 1; i < argc; ++i)
    {
        const QString option = QString::fromLocal8Bit(argv[i]);
        if (option == "--embedded") embedded = true;
        else if (option == "--python-home" && i + 1 < argc) home = QString::fromLocal8Bit(argv[++i]);
        else if (option == "--packages" && i + 1 < argc) packages = QString::fromLocal8Bit(argv[++i]);
        else return 2;
    }
    // Match the macOS product's requested desktop OpenGL surface.
    QSurfaceFormat format;
    format.setRenderableType(QSurfaceFormat::OpenGL);
    format.setVersion(2, 1);
    QSurfaceFormat::setDefaultFormat(format);
    auto application = std::make_unique<QApplication>(argc, argv);
    QApplication& app = *application; // argc/argv remain alive throughout main.
    QJsonObject report{{"status", "failed"}, {"embedded", embedded}, {"qtVersion", qVersion()},
                       {"platformPlugin", app.platformName()}, {"cppAppPointer", pointerText(&app)},
                       {"cppArguments", QJsonArray::fromStringList(app.arguments())}};
    checkpoint(report, "application-created");
    bool identity = app.platformName() == "cocoa" && !app.arguments().isEmpty();
    if (embedded)
    {
        if (home.isEmpty() || packages.isEmpty()) return 2;
        PyConfig config;
        PyConfig_InitIsolatedConfig(&config);
        config.parse_argv = 0;
        config.site_import = 0;
        config.write_bytecode = 0;
        config.install_signal_handlers = 0;
        const std::wstring wideHome = home.toStdWString();
        const std::wstring wideProgram = app.applicationFilePath().toStdWString();
        PyStatus status = PyConfig_SetString(&config, &config.home, wideHome.c_str());
        if (!PyStatus_Exception(status)) status = PyConfig_SetString(&config, &config.program_name, wideProgram.c_str());
        if (!PyStatus_Exception(status)) status = PyConfig_SetBytesArgv(&config, argc, argv);
        if (!PyStatus_Exception(status)) status = Py_InitializeFromConfig(&config);
        PyConfig_Clear(&config);
        if (PyStatus_Exception(status))
        {
            report["pythonError"] = status.err_msg ? status.err_msg : "Python initialization failed";
            checkpoint(report, "python-initialization-failed");
            return 4;
        }
        PyObject* path = PySys_GetObject("path");
        PyObject* packagePath = PyUnicode_FromString(packages.toUtf8().constData());
        const int inserted = packagePath ? PyList_Insert(path, 0, packagePath) : -1;
        Py_XDECREF(packagePath);
        if (inserted != 0 || !pythonRun(R"PY(
import json
import PySide6
from PySide6 import QtCore, QtWidgets
import shiboken6
_existing_app = QtWidgets.QApplication.instance()
_python_state_json = json.dumps({
    'pythonAppPointer': hex(shiboken6.getCppPointer(_existing_app)[0]) if _existing_app is not None else None,
    'pythonInstancePresent': _existing_app is not None,
    'pythonArguments': QtCore.QCoreApplication.arguments(),
    'pythonQtVersion': QtCore.qVersion(), 'pysideVersion': PySide6.__version__})
)PY", report))
        {
            checkpoint(report, "python-import-failed");
            return 4;
        }
        PyObject* globals = PyModule_GetDict(PyImport_AddModule("__main__"));
        PyObject* state = PyDict_GetItemString(globals, "_python_state_json");
        const char* raw = state ? PyUnicode_AsUTF8(state) : nullptr;
        if (!raw) return 4;
        const auto stateReport = QJsonDocument::fromJson(raw).object();
        for (auto i = stateReport.begin(); i != stateReport.end(); ++i) report[i.key()] = i.value();
        identity &= report["pythonInstancePresent"].toBool()
                    && report["pythonAppPointer"] == report["cppAppPointer"]
                    && report["pythonArguments"] == report["cppArguments"]
                    && report["pythonQtVersion"].toString() == "6.5.3"
                    && report["pysideVersion"].toString() == "6.5.3";
        report["sameApplicationInstance"] = identity;
        checkpoint(report, "python-application-observed");
        // Do not create a second Python QApplication to hide a split singleton.
        // Continue into the product's real WebEngine operation so a negative
        // fixture retains the actual fatal/exit status, not a warning heuristic.
        if (!pythonRun(R"PY(
from PySide6.QtWebEngineCore import QWebEngineProfile
_profile = QWebEngineProfile.defaultProfile()
_profile.httpUserAgent()
)PY", report))
        {
            checkpoint(report, "python-webengine-failed");
            return 4;
        }
        report["pythonDefaultProfileInitialized"] = true;
        checkpoint(report, "python-webengine-initialized");
    }
    else
    {
        QWebEngineProfile::defaultProfile()->httpUserAgent();
        report["cppDefaultProfileInitialized"] = true;
        checkpoint(report, "cpp-webengine-initialized");
    }
    bool loaded = false, sentinel = false, pixelMatched = false, timedOut = false;
    {
        QWebEngineView view;
        view.resize(128, 128);
        QObject::connect(&view, &QWebEngineView::loadFinished, &app, [&](bool ok) {
            loaded = ok;
            view.page()->runJavaScript("window.__openrv_mixed_probe === 'mixed-qt-6.5.3'", [&](const QVariant& value) {
                sentinel = value.toBool();
            });
        });
        QTimer poll;
        QObject::connect(&poll, &QTimer::timeout, &app, [&] {
            const bool exposed = view.windowHandle() && view.windowHandle()->isExposed();
            if (!loaded || !sentinel || !view.isVisible() || !exposed) return;
            const QImage image = view.grab().toImage();
            if (image.isNull()) return;
            const QColor pixel = image.pixelColor(image.width() / 2, image.height() / 2);
            pixelMatched = std::abs(pixel.red() - 32) <= 5 && std::abs(pixel.green() - 80) <= 5
                           && std::abs(pixel.blue() - 160) <= 5 && pixel.alpha() >= 250;
            report["widgetVisible"] = view.isVisible();
            report["windowExposed"] = exposed;
            report["pixel"] = QJsonArray{pixel.red(), pixel.green(), pixel.blue(), pixel.alpha()};
            report["imageWidth"] = image.width(); report["imageHeight"] = image.height();
            if (pixelMatched) app.quit();
        });
        QTimer::singleShot(20000, &app, [&] { timedOut = true; app.quit(); });
        poll.start(100);
        view.show();
        view.setHtml("<html><head><script>window.__openrv_mixed_probe='mixed-qt-6.5.3';</script></head>"
                     "<body style='margin:0;background:rgb(32,80,160);width:100vw;height:100vh'></body></html>");
        app.exec();
        report["loadFinished"] = loaded;
        report["javascriptSentinel"] = sentinel;
        report["visibleReadbackMatched"] = pixelMatched;
        report["timedOut"] = timedOut;
    }
    // Match the real macOS product: destroy the view, then QApplication while
    // Python wrappers still have a live VM, then finalize Python.
    checkpoint(report, "before-application-destruction");
    application.reset();
    report["applicationDestroyedBeforePythonFinalization"] = true;
    if (embedded)
    {
        const int finalized = Py_FinalizeEx();
        report["pythonFinalizedNormally"] = finalized == 0;
        if (finalized != 0) identity = false;
    }
    report["status"] = identity && loaded && sentinel && pixelMatched && !timedOut ? "passed" : "failed";
    checkpoint(report, "finished");
    return report["status"].toString() == "passed" ? 0 : 5;
}
