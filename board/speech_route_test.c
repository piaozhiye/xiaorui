#include <assert.h>
#include <stdio.h>
#include "speech_route.h"

int main(void) {
    for (int local = 0; local <= 1; ++local)
        for (int up = 0; up <= 1; ++up)
            for (int kws = 0; kws <= 1; ++kws)
                for (int guard = 0; guard <= 1; ++guard) {
                    xr_speech_route_t route = xr_speech_route(local, up, kws, guard);
                    if (local)
                        assert(route == (up && !kws && !guard
                                         ? XR_SPEECH_UPLOAD : XR_SPEECH_LOCAL));
                    else {
                        assert(route != XR_SPEECH_LOCAL);
                        assert(route == (!up ? XR_SPEECH_NO_UPLINK
                            : guard && !kws ? XR_SPEECH_ECHO_DROP : XR_SPEECH_UPLOAD));
                    }
                }
    puts("speech_route_test: OK (16 capability combinations)");
    return 0;
}
