/** @odoo-module **/

import { registry } from "@web/core/registry";
import { CharField, charField } from "@web/views/fields/char/char_field";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { useState } from "@odoo/owl";

export class PasswordEyeField extends CharField {
    static template = "skybox.PasswordEyeField";

    setup() {
        super.setup();
        this.eyeState = useState({ reveal: false });
        this.notification = useService("notification");
    }

    toggleReveal() {
        this.eyeState.reveal = !this.eyeState.reveal;
    }

    async copyValue() {
        const value = this.displayValue;
        if (!value) {
            return;
        }
        // navigator.clipboard n'existe qu'en HTTPS (ou localhost) : sur un
        // serveur en http, on passe par une zone de texte temporaire.
        let copied = false;
        try {
            if (navigator.clipboard && window.isSecureContext) {
                await navigator.clipboard.writeText(value);
                copied = true;
            }
        } catch {
            copied = false;
        }
        if (!copied) {
            copied = this.legacyCopy(value);
        }
        if (copied) {
            this.notification.add(_t("Cle copiee"), { type: "success" });
        } else {
            this.notification.add(_t("Impossible de copier la cle"), {
                type: "danger",
            });
        }
    }

    legacyCopy(value) {
        const textarea = document.createElement("textarea");
        textarea.value = value;
        textarea.setAttribute("readonly", "");
        textarea.style.position = "fixed";
        textarea.style.top = "-1000px";
        textarea.style.opacity = "0";
        document.body.appendChild(textarea);
        textarea.select();
        textarea.setSelectionRange(0, value.length);
        let ok = false;
        try {
            ok = document.execCommand("copy");
        } catch {
            ok = false;
        }
        document.body.removeChild(textarea);
        return ok;
    }

    get displayValue() {
        return this.props.record.data[this.props.name] || "";
    }

    onChange(ev) {
        this.props.record.update({ [this.props.name]: ev.target.value });
    }
}

export const passwordEyeField = {
    ...charField,
    component: PasswordEyeField,
    displayName: "Mot de passe (oeil)",
    supportedTypes: ["char"],
};

registry.category("fields").add("password_eye", passwordEyeField);
