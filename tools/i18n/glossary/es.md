# Loft glossary: Spanish (es)

Neutral Spanish for Spain and Latin America (the same text ships as es-ES and es-MX / es-419). Address the user
as *tú*. Buttons and menu items in the infinitive ("Continuar", "Vincular"); instructions in the *tú* imperative
("Ejecuta", "Abre", "Vuelve a intentarlo"). Avoid words that only work on one side of the Atlantic (no *ordenador*
/ *computadora*, *móvil* / *celular*, *fichero*, *vosotros*). Quotation marks: « ». Opening ¿ ¡ always. "solo"
without accent. Keep "…" as one character.

## Core terms

| English | Spanish | Notes |
|---|---|---|
| Loft gateway | el gateway de Loft | Masculine: *el gateway*, *tu gateway*, plural *gateways*. Kept in English (what IT users say; matches `loft-gateway`). Never *puerta de enlace* / *pasarela*. |
| gateway (UI heading/label) | Gateway | |
| pair / pairing | vincular / vinculación | "Pair" button = *Vincular*; "Pair with your gateway" = *Vincular con tu gateway*. |
| pairing link | enlace de vinculación | |
| pairing code | código de vinculación | The QR code / link payload. |
| key fingerprint | huella de la clave | Plural: *las huellas de sus claves*. Label: *Huella de la clave*. |
| keys | claves | |
| pinned (connection pinned to the key) | anclada a la clave | *conexión anclada a la clave de tu gateway* |
| end to end | de extremo a extremo | |
| at home | en casa | Status/route label: *En casa* |
| away / away from home | fuera de casa | Route label: *Fuera* (Windows/Android short label), *Fuera de casa* elsewhere |
| home network | la red de casa | |
| Can't reach home. | No hay conexión con casa. | |
| tunnel / tunnel provider | túnel / el proveedor del túnel | |
| direct (mode) | directo / modo directo | |
| port forward | redirección de puertos; *un puerto redirigido en tu router* | |
| router | router | |
| sign in / sign out | iniciar sesión / cerrar sesión | |
| sign-in (noun) | inicio de sesión | |
| Sign in again | Volver a iniciar sesión (button) / Vuelve a iniciar sesión. (message) | |
| sign-in code | código de acceso | Short and clear; *código de inicio de sesión* is too long. |
| Signed in as | Sesión iniciada como | |
| username / password | nombre de usuario / contraseña | |
| NAS account | cuenta del NAS | *NAS* is masculine: *el NAS*. |
| share / shared folder | carpeta compartida | "Shares" heading = *Carpetas compartidas*. |
| Loft folder | carpeta de Loft | |
| file / folder | archivo / carpeta | Never *fichero*. |
| upload / uploading / uploads | subir / Subiendo / Subidas | |
| download | descargar / descarga | |
| sync / syncing | sincronizar / Sincronizando | |
| Camera upload | Subida desde la cámara | |
| photos and videos | fotos y videos | *video* without accent (pan-Hispanic; RAE accepts both). |
| take photo | tomar foto | |
| photo library | biblioteca de fotos | |
| trash | papelera | "Move to trash" = *Mover a la papelera* |
| Save to Loft | Guardar en Loft | Share-sheet / share-menu label; website and store use exactly this. |
| Save here | Guardar aquí | |
| share sheet / share menu | menú Compartir | |
| Settings (in-app, Apple/Android) | Ajustes | Windows uses *Configuración* (the Windows term). |
| Check for updates | Buscar actualizaciones | |
| release notes | notas de la versión | |
| checksum | suma de comprobación (SHA-256) | |
| container / container image | contenedor / imagen de contenedor | |
| repository | repositorio | |
| issue (GitHub) | incidencia | |
| open source | código abierto | *gratis y de código abierto* |
| licence | licencia | |
| Support (site nav) | Ayuda | |
| Source (site nav) | Código fuente | |
| this phone / this iPhone | este teléfono / este iPhone | |
| this Mac | tu Mac | Avoids the *el Mac* (Spain) / *la Mac* (Latin America) article clash. |
| this PC | este equipo | Windows' own Spanish name for "This PC". |
| computer | equipo | Microsoft's neutral term; avoids *ordenador* / *computadora*. |
| phone | teléfono | |

## Platform words (official Spanish OS terms)

| English | Spanish |
|---|---|
| Finder / Finder sidebar | Finder / barra lateral del Finder |
| menu bar | barra de menús |
| System Settings (macOS) | Ajustes del Sistema |
| Settings › Loft (iOS) | Ajustes › Loft |
| Login Items & Extensions › File Providers | Ítems de inicio y extensiones › Proveedores de archivos |
| Applications | Aplicaciones |
| Files app (iOS and Android) | la app Archivos |
| Save to Files | Guardar en Archivos |
| Local Network | Red local |
| File Explorer / Explorer | Explorador de archivos / Explorador |
| navigation pane | panel de navegación |
| Always keep on this device | Mantener siempre en este dispositivo |
| More info › Run anyway (SmartScreen) | Más información › Ejecutar de todas formas |
| Office AutoSave / AutoRecover | Autoguardado / Autorrecuperación |
| Ctrl+S (save, in Office) | Ctrl+G (Spanish Office's save shortcut) |
| system file picker | selector de archivos del sistema |
| Wi-Fi | Wi-Fi |
| QR code | código QR |

## Tagline and name

- "Not the cloud. The loft." → **La nube, no. El desván.** *Loft* stays the name; *loft* the room is *desván*
  (the pan-Hispanic word for attic; *ático* means a penthouse flat in Spain).
- "Your files, kept at home." → **Tus archivos, en casa.**
- "Loft by Incremental", "Incremental AS": never translated.

## Apple's own labels (verified)

Read from Apple's own string tables on 2026-10-30: macOS 27.2 (System Settings.app InfoPlist `~mac`, GeneralSettings.appex, LoginItems.appex, FinderKit) and the iOS 27.1 simulator runtime (DocumentManagerUICore SaveToFiles.appex). Use them exactly, quotes included. **They override any row above that disagrees.**

| English | es |
|---|---|
| System Settings (macOS app name) | Ajustes del Sistema |
| General | General |
| Login Items & Extensions | Ítems de inicio y extensiones |
| File Providers | Proveedores de archivos |
| “Loft” is not enabled (Finder banner, FinderKit NE99, first sentence) | “Loft” no está activado |
| Enable (FinderKit NE100) | Activar |
| Save to Files (iOS share sheet) | Guardar en Archivos |
