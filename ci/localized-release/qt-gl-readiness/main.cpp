// Early host prerequisite check, not a substitute for the installed RV GUI tests.
#include <GL/glew.h>
#include <windows.h>
#include <QGuiApplication>
#include <QJsonDocument>
#include <QJsonObject>
#include <QOffscreenSurface>
#include <QOpenGLContext>
#include <QSurfaceFormat>
#include <QWindow>
#include <iostream>

using GetString = const GLubyte* (APIENTRY*)(GLenum);
static QString text(const GLubyte* value)
{
    return value ? QString::fromLatin1(reinterpret_cast<const char*>(value)) : QString();
}
static QJsonObject strings(GetString get)
{
    return {{"version", text(get(GL_VERSION))}, {"renderer", text(get(GL_RENDERER))}, {"vendor", text(get(GL_VENDOR))}};
}
static QString modulePath(HMODULE module)
{
    wchar_t path[32768] = {};
    return module && GetModuleFileNameW(module, path, 32768) ? QString::fromWCharArray(path) : QString();
}
int main(int argc, char** argv)
{
    QGuiApplication app(argc, argv);
    QSurfaceFormat format;
    format.setVersion(2, 1);
    format.setRenderableType(QSurfaceFormat::OpenGL);
    format.setDepthBufferSize(24);
    format.setStencilBufferSize(8);
    QOpenGLContext context;
    context.setFormat(format);
    QOffscreenSurface surface;
    QJsonObject report{{"status", "failed"}, {"realRuntimeVerified", false}, {"qtVersion", qVersion()},
                       {"platformPlugin", app.platformName()}, {"requestedMajor", 2}, {"requestedMinor", 1},
                       {"providerSelection", qEnvironmentVariableIsSet("OPENRV_CI_GL_PROVIDER") ? "hosted-software" : "default"},
                       {"glewInitCalled", false}};
    const bool created = context.create();
    report["contextCreated"] = created;
    if (created)
    {
        surface.setFormat(context.format());
        surface.create();
    }
    const bool current = created && surface.isValid() && context.makeCurrent(&surface);
    report["surfaceValid"] = surface.isValid();
    report["madeCurrent"] = current;
    int visible = 0;
    for (auto window : app.allWindows())
        if (window->isVisible())
            ++visible;
    report["visibleWindows"] = visible;
    bool ready = false;
    if (current && !visible)
    {
        auto qtGet = reinterpret_cast<GetString>(context.getProcAddress("glGetString"));
        if (qtGet)
        {
            report["qtGL"] = strings(qtGet);
            report["linkedCoreGL"] = strings(&glGetString);
            HMODULE qtOwner = nullptr;
            GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                               reinterpret_cast<LPCWSTR>(qtGet), &qtOwner);
            report["qtFunctionLibrary"] = modulePath(qtOwner);
            report["linkedCoreLibrary"] = modulePath(GetModuleHandleW(L"opengl32.dll"));
            const GLenum glewStatus = glewInit(nullptr);
            report["glewInitCalled"] = true;
            report["glewStatus"] = static_cast<int>(glewStatus);
            report["glewError"] = text(glewGetErrorString(glewStatus));
            report["glewVersion21"] = bool(GLEW_VERSION_2_1);
            report["actualMajor"] = context.format().majorVersion();
            report["actualMinor"] = context.format().minorVersion();
            report["openGLES"] = context.isOpenGLES();
            ready = glewStatus == GLEW_OK && GLEW_VERSION_2_1 && glGetString(GL_VERSION)
                    && !context.isOpenGLES() && context.format().majorVersion() >= 2;
        }
    }
    context.doneCurrent();
    surface.destroy();
    report["status"] = ready ? "passed" : "failed";
    std::cout << QJsonDocument(report).toJson(QJsonDocument::Indented).constData() << std::flush;
    return ready ? 0 : 3;
}
