export class MacroController {
    // private apiBase = 'http://localhost:8001';
    // private serial: string;
    // private macroPanel: HTMLDivElement | null = null;
    // private isRecording = false;

    constructor(_serial: string) {
        // this.serial = serial;
        this.initUI();
    }

    private initUI(): void {
        // Macro Widget Hidden as per user request (Step 1904)
        // logic retained but UI not injected
        return;

        /*
        this.macroPanel = document.createElement('div');
        this.macroPanel.id = 'macro-controls';
        // ... (rest of the UI code) ...
        document.body.appendChild(this.macroPanel); // Disabled
        
        this.attachEvents();
        this.makeDraggable();
        */
    }

    /*
    private attachEvents(): void {
        const btnToggle = document.getElementById('btn-toggle-rec');
        // ... (omitted) ...
        apkInput?.addEventListener('change', (e) => this.handleInstall(e));
    }

    private async handleInstall(event: Event): Promise<void> {
       // ...
    }

    private async handleDocumentPip(): Promise<void> {
       // ...
    }

    private openMacroList(): void {
        alert("매크로 목록은 대시보드(메인화면)에서 확인해주세요.\n(추후 이 패널에 통합 예정)");
    }

    private async handleToggleRecord(btn: HTMLElement): Promise<void> {
       // ...
    }

    private makeDraggable(): void {
        const handle = document.getElementById('drag-handle');
        if (!handle || !this.macroPanel) return;
        // ...
    }
    */
}
