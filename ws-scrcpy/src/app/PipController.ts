/**
 * Floating Picture-in-Picture control for the stream page.
 *
 * Watching a long test run means keeping a device visible while working in
 * another window, and stacking several of them when more than one device is
 * under test. Native PiP already gives an always-on-top view, so this only has
 * to find the surface the active decoder draws to and hand it over.
 *
 * MSE draws into a <video>, which PiP accepts directly. The canvas-based
 * decoders (Broadway, TinyH264, WebCodecs, MJPEG) need a bridge, because PiP
 * only takes video elements: captureStream() turns the canvas into a
 * MediaStream that a hidden <video> can carry into the PiP window.
 */
export class PipController {
    // Both MsePlayer and BaseCanvasBasedPlayer tag their surface with this class.
    private static readonly SURFACE = 'video.video-layer, canvas.video-layer';
    private static readonly CAPTURE_FPS = 30;

    private button: HTMLButtonElement | null = null;
    private status: HTMLSpanElement | null = null;
    private bridge: HTMLVideoElement | null = null;
    private statusTimer = 0;

    constructor(private readonly serial: string) {
        // Firefox has no PiP and Safari's is partial. A button that cannot work
        // is worse than no button, so feature-detect before injecting anything.
        if (!document.pictureInPictureEnabled) {
            return;
        }
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', () => this.inject(), { once: true });
        } else {
            this.inject();
        }
    }

    private inject(): void {
        if (document.getElementById('pip-controls')) {
            return;
        }

        const box = document.createElement('div');
        box.id = 'pip-controls';
        box.style.cssText = [
            'position:fixed',
            'right:10px',
            'bottom:10px',
            'z-index:2147483000',
            'display:flex',
            'flex-direction:column',
            'align-items:flex-end',
            'gap:4px',
            'font-family:monospace',
            'font-size:11px',
        ].join(';');

        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = 'PiP';
        button.title = `${this.serial} 화면을 항상 위에 띄웁니다`;
        button.style.cssText = [
            'padding:5px 10px',
            'background:rgba(0,0,0,0.72)',
            'color:#e0e0e0',
            'border:1px solid #4a9eff',
            'border-radius:4px',
            'cursor:pointer',
            'font:inherit',
        ].join(';');
        button.addEventListener('click', () => {
            void this.toggle();
        });

        const status = document.createElement('span');
        status.style.cssText = 'color:#ff9f43; max-width:220px; text-align:right;';

        box.appendChild(status);
        box.appendChild(button);
        document.body.appendChild(box);

        this.button = button;
        this.status = status;
    }

    private async toggle(): Promise<void> {
        try {
            if (document.pictureInPictureElement) {
                await document.exitPictureInPicture();
                return;
            }

            const surface = document.querySelector(PipController.SURFACE);
            if (!surface) {
                // The player element appears only once the stream negotiates, so
                // an early click is normal rather than an error.
                this.say('스트림이 아직 준비되지 않았습니다');
                return;
            }

            if (surface instanceof HTMLVideoElement) {
                // The <video> is in the DOM before any frame arrives. Asking for
                // PiP then fails with a raw DOMException about missing metadata,
                // so check first and say something the operator can act on.
                if (surface.readyState === surface.HAVE_NOTHING) {
                    this.say('스트림이 아직 준비되지 않았습니다');
                    return;
                }
                await surface.requestPictureInPicture();
                this.entered(surface);
                return;
            }

            const bridge = await this.bridgeCanvas(surface as HTMLCanvasElement);
            await bridge.requestPictureInPicture();
            this.entered(bridge);
        } catch (e) {
            this.setActive(false);
            this.releaseBridge();
            this.say(`PiP 실패: ${e instanceof Error ? e.message : String(e)}`);
        }
    }

    private entered(video: HTMLVideoElement): void {
        this.say('');
        this.setActive(true);
        // The window can also be closed from the PiP window's own controls, so
        // the button has to follow the browser rather than its own click count.
        video.addEventListener(
            'leavepictureinpicture',
            () => {
                this.setActive(false);
                this.releaseBridge();
            },
            { once: true },
        );
    }

    private async bridgeCanvas(canvas: HTMLCanvasElement): Promise<HTMLVideoElement> {
        if (!this.bridge) {
            const video = document.createElement('video');
            video.muted = true;
            video.playsInline = true;
            // Present in the document but out of the way: it exists only as a
            // carrier for the canvas stream.
            video.style.cssText = 'position:fixed; width:1px; height:1px; opacity:0; pointer-events:none;';
            document.body.appendChild(video);
            this.bridge = video;
        }
        if (!this.bridge.srcObject) {
            this.bridge.srcObject = canvas.captureStream(PipController.CAPTURE_FPS);
        }
        await this.bridge.play();
        return this.bridge;
    }

    private releaseBridge(): void {
        const video = this.bridge;
        if (!video) {
            return;
        }
        const stream = video.srcObject;
        if (stream instanceof MediaStream) {
            stream.getTracks().forEach((track) => track.stop());
        }
        video.srcObject = null;
        video.remove();
        this.bridge = null;
    }

    private setActive(active: boolean): void {
        if (!this.button) {
            return;
        }
        this.button.textContent = active ? 'PiP 종료' : 'PiP';
        this.button.style.borderColor = active ? '#4ade80' : '#4a9eff';
    }

    private say(message: string): void {
        if (!this.status) {
            return;
        }
        this.status.textContent = message;
        window.clearTimeout(this.statusTimer);
        if (message) {
            this.statusTimer = window.setTimeout(() => {
                if (this.status) {
                    this.status.textContent = '';
                }
            }, 5000);
        }
    }
}
