; Instalador del Renovador de Sesión — GestionSLA
; Lo compila el workflow .github/workflows/build_renovador.yml (Inno Setup 6).
; Se instala para el usuario actual (no pide permisos de administrador).

#ifndef AppVersion
  #define AppVersion "2.0.0"
#endif

[Setup]
AppId={{D484C3BB-BCDE-43C6-94DE-8CAD90A11EC7}
AppName=Renovador de Sesión GestionSLA
AppVersion={#AppVersion}
AppPublisher=GestionSLA
DefaultDirName={localappdata}\Programs\RenovadorSesionGestionSLA
DefaultGroupName=GestionSLA
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\salida
OutputBaseFilename=Instalador_RenovadorSesion_GestionSLA
SetupIconFile=icono.ico
UninstallDisplayIcon={app}\RenovadorSesionGestionSLA.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Tasks]
Name: "desktopicon"; Description: "Crear un acceso directo en el escritorio"; GroupDescription: "Accesos directos:"

[Files]
Source: "..\dist\RenovadorSesionGestionSLA\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Renovador de Sesión GestionSLA"; Filename: "{app}\RenovadorSesionGestionSLA.exe"
Name: "{group}\Desinstalar Renovador de Sesión"; Filename: "{uninstallexe}"
Name: "{userdesktop}\Renovador de Sesión GestionSLA"; Filename: "{app}\RenovadorSesionGestionSLA.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\RenovadorSesionGestionSLA.exe"; Description: "Abrir el Renovador de Sesión ahora"; Flags: nowait postinstall skipifsilent
