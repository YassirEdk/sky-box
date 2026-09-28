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
        try {
            await navigator.clipboard.writeText(value);
            this.notification.add(_t("Cle copiee"), { type: "success" });
        } catch {
            this.notification.add(_t("Impossible de copier la cle"), {
                type: "danger",
            });
        }
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
