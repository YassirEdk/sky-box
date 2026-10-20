/** @odoo-module **/
/**
 * Affiche toutes les dates a l'heure du serveur (fuseau de Parametres >
 * Skybox), quel que soit le fuseau de l'ordinateur.
 */
import { session } from "@web/session";

const { Settings, IANAZone, FixedOffsetZone } = luxon;
const serverTz = session.skybox_tz;

if (serverTz && serverTz.name) {
    let zone = IANAZone.create(serverTz.name);
    // Base de fuseaux du navigateur perimee : decalage donne par le serveur.
    if (!zone.isValid || zone.offset(Date.now()) !== serverTz.offset) {
        zone = FixedOffsetZone.instance(serverTz.offset);
    }
    Settings.defaultZone = zone;
}
