// Native hosted-runner diagnostics only. This does not verify an OpenRV product.
#include <GL/glew.h>
#include <OpenGL/OpenGL.h>
#include <OpenGL/CGLRenderers.h>
#include <TwkGLFFBO/MacCGLContext.h>
#include <QApplication>
#include <QColor>
#include <QImage>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QOpenGLContext>
#include <QOpenGLWidget>
#include <QSurfaceFormat>
#include <QTimer>
#include <QWindow>
#include <algorithm>
#include <cmath>
#include <iostream>
#include <string>
#include <utility>
#include <vector>

using A = CGLPixelFormatAttribute;
static A attr(int value) { return static_cast<A>(value); }
static QString gltext(const GLubyte* value)
{
    return value ? QString::fromLatin1(reinterpret_cast<const char*>(value)) : QString();
}
static QString cgltext(CGLError value) { return QString::fromLatin1(CGLErrorString(value)); }
static void emitReport(const QJsonObject& report)
{
    std::cout << QJsonDocument(report).toJson(QJsonDocument::Indented).constData() << std::flush;
}
static QJsonObject baseReport(const QString& mode)
{
    return {{"check", "native-macos-graphics-diagnostic"}, {"mode", mode},
            {"productRuntimeVerified", false}, {"qtVersion", qVersion()},
            {"unsignedLongBytes", int(sizeof(unsigned long))}, {"cglAttributeBytes", int(sizeof(A))},
            {"bundledGlewSourceSha256", OPENRV_GLEW_SOURCE_SHA256},
            {"productionContextPolicySha256", OPENRV_CGL_POLICY_SHA256}};
}
static QJsonArray renderers()
{
    CGLRendererInfoObj info = nullptr;
    GLint count = 0;
    const CGLError error = CGLQueryRendererInfo(0xffffffffu, &info, &count);
    QJsonArray result;
    if (error != kCGLNoError || !info)
        result.append(QJsonObject{{"queryError", int(error)}, {"message", cgltext(error)}});
    else
        for (GLint index = 0; index < count; ++index)
        {
            QJsonObject item{{"index", index}};
            for (const auto& property : std::vector<std::pair<const char*, CGLRendererProperty>>{
                     {"rendererId", kCGLRPRendererID}, {"accelerated", kCGLRPAccelerated}, {"online", kCGLRPOnline}})
            {
                GLint value = 0;
                const CGLError status = CGLDescribeRenderer(info, index, property.second, &value);
                item[property.first] = value;
                item[QString::fromLatin1(property.first) + "Error"] = int(status);
            }
            result.append(item);
        }
    if (info) CGLDestroyRendererInfo(info);
    return result;
}
static GLuint shader(GLenum type, const char* source, QString& error)
{
    const GLuint object = glCreateShader(type);
    glShaderSource(object, 1, &source, nullptr);
    glCompileShader(object);
    GLint ok = GL_FALSE;
    glGetShaderiv(object, GL_COMPILE_STATUS, &ok);
    if (!ok)
    {
        char log[4096] = {};
        glGetShaderInfoLog(object, sizeof(log), nullptr, log);
        error = QString::fromUtf8(log);
        glDeleteShader(object);
        return 0;
    }
    return object;
}
static GLuint program(QString& error)
{
    const GLuint vertex = shader(GL_VERTEX_SHADER,
        "#version 120\nvoid main(){gl_Position=gl_Vertex;}\n", error);
    if (!vertex) return 0;
    const GLuint fragment = shader(GL_FRAGMENT_SHADER,
        "#version 120\nvoid main(){gl_FragColor=vec4(0.125,0.5,0.75,1.0);}\n", error);
    if (!fragment) { glDeleteShader(vertex); return 0; }
    const GLuint linked = glCreateProgram();
    glAttachShader(linked, vertex);
    glAttachShader(linked, fragment);
    glLinkProgram(linked);
    glDeleteShader(vertex);
    glDeleteShader(fragment);
    GLint ok = GL_FALSE;
    glGetProgramiv(linked, GL_LINK_STATUS, &ok);
    if (!ok)
    {
        char log[4096] = {};
        glGetProgramInfoLog(linked, sizeof(log), nullptr, log);
        error = QString::fromUtf8(log);
        glDeleteProgram(linked);
        return 0;
    }
    return linked;
}
static void quad(GLuint linked, int width, int height)
{
    glViewport(0, 0, width, height);
    for (GLenum capability : {GL_BLEND, GL_DEPTH_TEST, GL_STENCIL_TEST, GL_SCISSOR_TEST, GL_CULL_FACE, GL_DITHER})
        glDisable(capability);
    glClearColor(0, 0, 0, 1);
    glClear(GL_COLOR_BUFFER_BIT);
    glUseProgram(linked);
    glBegin(GL_QUADS);
    glVertex2f(-1, -1); glVertex2f(1, -1); glVertex2f(1, 1); glVertex2f(-1, 1);
    glEnd();
    glUseProgram(0);
    glFinish();
}
static QJsonObject floatReadback(GLenum format, const char* name, GLuint linked)
{
    QJsonArray priorErrors;
    for (int i = 0; i < 16; ++i)
    {
        const GLenum error = glGetError();
        if (error == GL_NO_ERROR) break;
        priorErrors.append(int(error));
    }
    GLint oldFbo = 0, oldRenderbuffer = 0, viewport[4] = {};
    glGetIntegerv(GL_FRAMEBUFFER_BINDING_EXT, &oldFbo);
    glGetIntegerv(GL_RENDERBUFFER_BINDING_EXT, &oldRenderbuffer);
    glGetIntegerv(GL_VIEWPORT, viewport);
    GLuint fbo = 0, buffer = 0;
    glGenFramebuffersEXT(1, &fbo);
    glGenRenderbuffersEXT(1, &buffer);
    glBindFramebufferEXT(GL_FRAMEBUFFER_EXT, fbo);
    glBindRenderbufferEXT(GL_RENDERBUFFER_EXT, buffer);
    glRenderbufferStorageEXT(GL_RENDERBUFFER_EXT, format, 4, 4);
    glFramebufferRenderbufferEXT(GL_FRAMEBUFFER_EXT, GL_COLOR_ATTACHMENT0_EXT, GL_RENDERBUFFER_EXT, buffer);
    const GLenum status = glCheckFramebufferStatusEXT(GL_FRAMEBUFFER_EXT);
    GLfloat pixel[4] = {};
    bool matched = false;
    if (status == GL_FRAMEBUFFER_COMPLETE_EXT)
    {
        quad(linked, 4, 4);
        glReadPixels(2, 2, 1, 1, GL_RGBA, GL_FLOAT, pixel);
        matched = std::abs(pixel[0] - 0.125f) < 0.002f && std::abs(pixel[1] - 0.5f) < 0.002f
               && std::abs(pixel[2] - 0.75f) < 0.002f && std::abs(pixel[3] - 1.0f) < 0.002f;
    }
    const GLenum error = glGetError();
    glBindFramebufferEXT(GL_FRAMEBUFFER_EXT, oldFbo);
    glBindRenderbufferEXT(GL_RENDERBUFFER_EXT, oldRenderbuffer);
    glViewport(viewport[0], viewport[1], viewport[2], viewport[3]);
    glDeleteRenderbuffersEXT(1, &buffer);
    glDeleteFramebuffersEXT(1, &fbo);
    return {{"format", name}, {"status", matched && error == GL_NO_ERROR ? "passed" : "failed"},
            {"framebufferStatus", int(status)}, {"glError", int(error)}, {"pixelMatched", matched},
            {"priorGlErrors", priorErrors},
            {"pixel", QJsonArray{pixel[0], pixel[1], pixel[2], pixel[3]}}};
}
static QJsonObject exerciseCurrent()
{
    QJsonObject report{{"contextCurrent", CGLGetCurrentContext() != nullptr},
                       {"version", gltext(glGetString(GL_VERSION))},
                       {"renderer", gltext(glGetString(GL_RENDERER))}, {"vendor", gltext(glGetString(GL_VENDOR))},
                       {"glslVersion", gltext(glGetString(GL_SHADING_LANGUAGE_VERSION))}};
    const GLenum status = glewInit(nullptr); // The same bundled GLEW API used by OpenRV.
    report["glewStatus"] = int(status);
    report["glewError"] = gltext(glewGetErrorString(status));
    report["glewVersion21"] = bool(GLEW_VERSION_2_1);
    report["extFramebufferObject"] = bool(GLEW_EXT_framebuffer_object);
    report["rvioContextReady"] = false;
    report["allFloatFormatsPassed"] = false;
    for (int i = 0; i < 16 && glGetError() != GL_NO_ERROR; ++i) {}
    if (status != GLEW_OK || !GLEW_VERSION_2_1 || !GLEW_EXT_framebuffer_object) return report;
    QString error;
    const GLuint linked = program(error);
    report["glsl120Linked"] = bool(linked);
    report["shaderError"] = error;
    if (!linked) return report;
    QJsonArray tests;
    bool all = true, rgb16 = false;
    for (const auto& entry : std::vector<std::pair<GLenum, const char*>>{
             {GL_RGB16F_ARB, "RGB16F"}, {GL_RGBA16F_ARB, "RGBA16F"}, {GL_RGBA32F_ARB, "RGBA32F"}})
    {
        const auto result = floatReadback(entry.first, entry.second, linked);
        const bool passed = result["status"].toString() == "passed";
        if (entry.first == GL_RGB16F_ARB) rgb16 = passed;
        all &= passed;
        tests.append(result);
    }
    glDeleteProgram(linked);
    report["floatFboTests"] = tests;
    report["rvioContextReady"] = rgb16;
    report["allFloatFormatsPassed"] = all;
    return report;
}
static QJsonObject cglCase(const QString& name, const A* attributes)
{
    std::cerr << "Testing CGL case: " << name.toStdString() << std::endl;
    CGLPixelFormatObj pixel = nullptr;
    CGLContextObj context = nullptr;
    GLint count = 0;
    QJsonObject report{{"name", name}, {"status", "failed"}};
    CGLError error = CGLChoosePixelFormat(attributes, &pixel, &count);
    report["chooseError"] = int(error);
    report["chooseMessage"] = cgltext(error);
    report["pixelFormatCount"] = count;
    if (error == kCGLNoError && pixel && count > 0)
    {
        for (const auto& field : std::vector<std::pair<const char*, A>>{
                 {"selectedRendererId", kCGLPFARendererID}, {"selectedAccelerated", kCGLPFAAccelerated}})
        {
            GLint value = 0;
            const CGLError status = CGLDescribePixelFormat(pixel, 0, field.second, &value);
            report[field.first] = value;
            report[QString::fromLatin1(field.first) + "Error"] = int(status);
        }
        error = CGLCreateContext(pixel, nullptr, &context);
        report["createError"] = int(error);
        report["createMessage"] = cgltext(error);
        if (error == kCGLNoError && context)
        {
            error = CGLSetCurrentContext(context);
            report["currentError"] = int(error);
            report["currentMessage"] = cgltext(error);
            if (error == kCGLNoError)
            {
                const auto gl = exerciseCurrent();
                report["gl"] = gl;
                report["status"] = gl["rvioContextReady"].toBool() ? "ready" : "failed";
            }
        }
    }
    CGLSetCurrentContext(nullptr);
    if (context) CGLDestroyContext(context);
    if (pixel) CGLDestroyPixelFormat(pixel);
    return report;
}
static QJsonObject productionPolicy()
{
    auto selected = TwkGLF::MacCGL::createContext();
    QJsonObject report{{"name", "production-policy"}, {"status", "failed"},
                       {"selectedCandidate", selected.selectedCandidate ? QJsonValue(selected.selectedCandidate) : QJsonValue()},
                       {"contextCurrent", selected.context && CGLGetCurrentContext() == selected.context}};
    QJsonArray attempts;
    for (std::size_t index = 0; index < selected.attemptCount; ++index)
    {
        const auto& attempt = selected.attempts[index];
        QJsonObject item{{"name", attempt.name}, {"chooseError", int(attempt.chooseError)},
                         {"chooseMessage", cgltext(attempt.chooseError)},
                         {"pixelFormatCount", attempt.pixelFormatCount},
                         {"pixelFormatValid", attempt.pixelFormatValid}, {"contextCreated", attempt.contextCreated}};
        if (attempt.createAttempted)
        {
            item["createError"] = int(attempt.createError);
            item["createMessage"] = cgltext(attempt.createError);
        }
        if (attempt.currentAttempted)
        {
            item["currentError"] = int(attempt.currentError);
            item["currentMessage"] = cgltext(attempt.currentError);
            item["currentMatches"] = attempt.currentMatches;
        }
        if (attempt.restoreAttempted) item["restoreError"] = int(attempt.restoreError);
        attempts.append(item);
    }
    report["attempts"] = attempts;
    if (report["contextCurrent"].toBool())
    {
        const auto gl = exerciseCurrent();
        report["gl"] = gl;
        report["status"] = gl["rvioContextReady"].toBool() ? "ready" : "failed";
    }
    TwkGLF::MacCGL::destroy(selected.context, selected.pixelFormat);
    return report;
}
static int cglDiagnostic()
{
    QJsonObject report = baseReport("cgl");
    report["renderers"] = renderers();
    QJsonArray cases;
    // Intentionally reproduce the actual upstream ABI cast as a diagnostic case.
    unsigned long original[] = {kCGLPFAAccelerated, kCGLPFAColorFloat, kCGLPFAColorSize, 48,
                                kCGLPFAAlphaSize, 16, 0};
    cases.append(cglCase("original-unsigned-long-hardware", reinterpret_cast<const A*>(original)));
    const std::vector<std::pair<const char*, std::vector<A>>> typed = {
        {"typed-hardware-float", {kCGLPFAAccelerated, kCGLPFAColorFloat, kCGLPFAColorSize, attr(48), kCGLPFAAlphaSize, attr(16), attr(0)}},
        {"typed-hardware-rgba8", {kCGLPFAAccelerated, kCGLPFAColorSize, attr(24), kCGLPFAAlphaSize, attr(8), attr(0)}},
        {"typed-apple-software-float", {kCGLPFAAllRenderers, kCGLPFARendererID, attr(kCGLRendererAppleSWID), kCGLPFAColorFloat,
                                      kCGLPFAColorSize, attr(96), kCGLPFAAlphaSize, attr(32), attr(0)}},
        {"typed-generic-float-renderer", {kCGLPFAAllRenderers, kCGLPFARendererID, attr(kCGLRendererGenericFloatID), attr(0)}},
        {"typed-minimal", {attr(0)}},
        {"typed-offline", {kCGLPFAAllowOfflineRenderers, attr(0)}}};
    for (const auto& entry : typed) cases.append(cglCase(entry.first, entry.second.data()));
    cases.append(productionPolicy());
    bool ready = false;
    for (const auto& entry : cases) ready |= entry.toObject()["status"].toString() == "ready";
    report["cases"] = cases;
    report["rvioCandidateReady"] = ready;
    report["status"] = "diagnostic-collected";
    emitReport(report);
    return ready ? 0 : 3;
}

class ProbeWidget : public QOpenGLWidget
{
public:
    QJsonObject report;
    bool finished = false;
    GLuint linked = 0;
    bool scheduled = false;
    explicit ProbeWidget(QWidget* parent = nullptr) : QOpenGLWidget(parent) {}
    ~ProbeWidget() override
    {
        if (linked && context() && context()->isValid()) { makeCurrent(); glDeleteProgram(linked); doneCurrent(); }
    }
    void finish(bool timeout = false)
    {
        if (finished) return;
        finished = true;
        report["timedOut"] = timeout;
        report["widgetVisible"] = isVisible();
        report["widgetValid"] = isValid();
        const bool exposed = windowHandle() && windowHandle()->isExposed();
        report["windowExposed"] = exposed;
        bool matched = false;
        if (!timeout && isValid())
        {
            const QImage image = grabFramebuffer();
            report["imageWidth"] = image.width(); report["imageHeight"] = image.height();
            if (!image.isNull())
            {
                const QColor pixel = image.pixelColor(image.width() / 2, image.height() / 2);
                report["widgetPixel"] = QJsonArray{pixel.red(), pixel.green(), pixel.blue(), pixel.alpha()};
                matched = std::abs(pixel.red() - 32) <= 3 && std::abs(pixel.green() - 128) <= 3
                       && std::abs(pixel.blue() - 191) <= 3 && pixel.alpha() >= 252;
            }
        }
        report["visibleReadbackMatched"] = matched;
        report["status"] = !timeout && isVisible() && exposed && isValid() && linked && matched
                            && !report["openGLES"].toBool(true) && QApplication::platformName() == "cocoa"
                            && report["gl"].toObject()["allFloatFormatsPassed"].toBool() ? "passed" : "failed";
        QApplication::quit();
    }
protected:
    void initializeGL() override
    {
        report["gl"] = exerciseCurrent();
        if (context())
        {
            report["actualMajor"] = context()->format().majorVersion();
            report["actualMinor"] = context()->format().minorVersion();
            report["actualDepth"] = context()->format().depthBufferSize();
            report["actualStencil"] = context()->format().stencilBufferSize();
            report["openGLES"] = context()->isOpenGLES();
        }
        if (report["gl"].toObject()["glewStatus"].toInt(-1) == GLEW_OK && GLEW_VERSION_2_1)
        {
            QString error;
            linked = program(error);
            report["widgetShaderError"] = error;
        }
    }
    void paintGL() override
    {
        if (linked) quad(linked, int(width() * devicePixelRatioF()), int(height() * devicePixelRatioF()));
        if (!scheduled) { scheduled = true; QTimer::singleShot(100, this, [this] { finish(); }); }
    }
};
int main(int argc, char** argv)
{
    const QString mode = argc == 2 ? QString::fromLocal8Bit(argv[1]) : QString();
    if (mode == "cgl") return cglDiagnostic();
    if (mode != "qt-default" && mode != "qt-software") return 2;
    const bool software = mode == "qt-software";
    if (software) QApplication::setAttribute(Qt::AA_UseSoftwareOpenGL);
    QSurfaceFormat format;
    format.setVersion(2, 1);
    format.setRenderableType(QSurfaceFormat::OpenGL);
    format.setDepthBufferSize(24);
    format.setStencilBufferSize(8);
    QSurfaceFormat::setDefaultFormat(format);
    QApplication app(argc, argv);
    ProbeWidget widget;
    widget.setFormat(format);
    widget.report = baseReport(mode);
    widget.report["status"] = "failed";
    widget.report["softwareAttributeBeforeApplication"] = software;
    widget.report["platformPlugin"] = app.platformName();
    widget.report["requestedMajor"] = 2; widget.report["requestedMinor"] = 1;
    widget.report["requestedDepth"] = 24; widget.report["requestedStencil"] = 8;
    widget.resize(96, 96);
    widget.show(); // Deliberately test a real visible widget, as the product GUI does.
    QTimer::singleShot(15000, &widget, [&widget] { widget.finish(true); });
    app.exec();
    emitReport(widget.report);
    return widget.report["status"].toString() == "passed" ? 0 : 3;
}
