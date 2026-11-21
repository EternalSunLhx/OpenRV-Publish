// SPDX-License-Identifier: Apache-2.0
#ifndef TWKGLFFBO_MAC_CGL_CONTEXT_H
#define TWKGLFFBO_MAC_CGL_CONTEXT_H

#include <OpenGL/OpenGL.h>
#include <OpenGL/CGLRenderers.h>
#include <array>
#include <cstddef>

namespace TwkGLF::MacCGL
{
    struct Attempt
    {
        const char* name = nullptr;
        CGLError chooseError = kCGLNoError;
        GLint pixelFormatCount = 0;
        bool pixelFormatValid = false;
        bool createAttempted = false;
        CGLError createError = kCGLNoError;
        bool contextCreated = false;
        bool currentAttempted = false;
        CGLError currentError = kCGLNoError;
        bool currentMatches = false;
        bool restoreAttempted = false;
        CGLError restoreError = kCGLNoError;
    };

    struct Context
    {
        // Plain owning handles: caller transfers them or calls destroy().
        CGLPixelFormatObj pixelFormat = nullptr;
        CGLContextObj context = nullptr;
        GLint pixelFormatCount = 0;
        const char* selectedCandidate = nullptr;
        std::array<Attempt, 3> attempts{};
        std::size_t attemptCount = 0;
    };

    inline void destroy(CGLContextObj& context, CGLPixelFormatObj& pixelFormat)
    {
        if (context)
        {
            if (CGLGetCurrentContext() == context) CGLSetCurrentContext(nullptr);
            CGLDestroyContext(context);
            context = nullptr;
        }
        if (pixelFormat)
        {
            CGLDestroyPixelFormat(pixelFormat);
            pixelFormat = nullptr;
        }
    }

    inline Context createContext(bool softwareOnly = false)
    {
        using A = CGLPixelFormatAttribute;
        const A hardwareFloat[] = {kCGLPFAAccelerated, kCGLPFAColorFloat, kCGLPFAColorSize,
                                   static_cast<A>(48), kCGLPFAAlphaSize, static_cast<A>(16), static_cast<A>(0)};
        // The context's default drawable need not be float: OpenRV renders
        // into its separate RGB/RGBA16F FBO, whose format remains unchanged.
        const A hardwareRgba8[] = {kCGLPFAAccelerated, kCGLPFAColorSize, static_cast<A>(24),
                                   kCGLPFAAlphaSize, static_cast<A>(8), static_cast<A>(0)};
        const A genericFloat[] = {kCGLPFAAllRenderers, kCGLPFARendererID,
                                  static_cast<A>(kCGLRendererGenericFloatID), static_cast<A>(0)};
        struct Candidate { const char* name; const A* attributes; };
        const Candidate candidates[] = {{"typed-hardware-float", hardwareFloat},
                                         {"typed-hardware-rgba8", hardwareRgba8},
                                         {"typed-generic-float-renderer", genericFloat}};
        Context result;
        CGLContextObj previous = CGLGetCurrentContext();
        if (previous) CGLRetainContext(previous);
        for (std::size_t index = softwareOnly ? 2 : 0; index < 3; ++index)
        {
            Attempt& attempt = result.attempts[result.attemptCount++];
            attempt.name = candidates[index].name;
            CGLPixelFormatObj pixel = nullptr;
            CGLContextObj context = nullptr;
            attempt.chooseError = CGLChoosePixelFormat(candidates[index].attributes, &pixel, &attempt.pixelFormatCount);
            attempt.pixelFormatValid = pixel != nullptr;
            if (attempt.chooseError == kCGLNoError && pixel && attempt.pixelFormatCount > 0)
            {
                attempt.createAttempted = true;
                attempt.createError = CGLCreateContext(pixel, nullptr, &context);
                attempt.contextCreated = context != nullptr;
                if (attempt.createError == kCGLNoError && context)
                {
                    attempt.currentAttempted = true;
                    attempt.currentError = CGLSetCurrentContext(context);
                    attempt.currentMatches = CGLGetCurrentContext() == context;
                    if (attempt.currentError == kCGLNoError && attempt.currentMatches)
                    {
                        result.pixelFormat = pixel;
                        result.context = context;
                        result.pixelFormatCount = attempt.pixelFormatCount;
                        result.selectedCandidate = candidates[index].name;
                        if (previous) CGLReleaseContext(previous);
                        return result;
                    }
                }
            }
            destroy(context, pixel);
            if (CGLGetCurrentContext() != previous)
            {
                attempt.restoreAttempted = true;
                attempt.restoreError = CGLSetCurrentContext(previous);
            }
        }
        if (previous) CGLReleaseContext(previous);
        return result;
    }
}
#endif
