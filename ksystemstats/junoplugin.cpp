// SPDX-License-Identifier: GPL-2.0-or-later
// SPDX-FileCopyrightText: 2026 Marco Barbone

#include "junoplugin.h"

#include <QDebug>
#include <QDir>
#include <QElapsedTimer>
#include <QFileInfo>
#include <QProcess>
#include <QRegularExpression>

#include <KLocalizedString>
#include <KPluginFactory>

#include <systemstats/SensorContainer.h>
#include <systemstats/SensorObject.h>
#include <systemstats/SensorProperty.h>

namespace
{

// Env hooks so the container gate can drive the plugin against fixtures:
// JUNO_KSS_SYSFS (default /sys), JUNO_KSS_PROC_STAT, JUNO_KSS_DGPU_PCI,
// JUNO_KSS_NVIDIA_SMI. Production never sets them.
QString pathEnv(const char *name, const QString &fallback)
{
    const QByteArray v = qgetenv(name);
    return v.isEmpty() ? fallback : QString::fromLocal8Bit(v);
}

QString readFile(const QString &path)
{
    QFile f(path);
    if (!f.open(QIODevice::ReadOnly | QIODevice::Text))
        return QString();
    return QString::fromLocal8Bit(f.readAll()).trimmed();
}

double readNumber(const QString &path, bool &ok)
{
    const QString s = readFile(path);
    if (s.isEmpty()) {
        ok = false;
        return 0.0;
    }
    return s.section(QRegularExpression(QStringLiteral("\\s")), 0, 0).toDouble(&ok);
}

// Scan <platformDir>/NAME/hwmon/hwmonN. hwmon's <name> on clevofan is the board
// codename (unknown ahead of time), so empty hwmonName means "the device dir
// itself is enough".
QString findHwmon(const QString &platformDir, const QString &deviceName, const QString &hwmonName)
{
    const QString base = platformDir + QStringLiteral("/") + deviceName;
    const QDir hwmons(base + QStringLiteral("/hwmon"));
    for (const QString &entry : hwmons.entryList(QDir::Dirs | QDir::NoDotAndDotDot)) {
        const QString dir = hwmons.filePath(entry);
        if (hwmonName.isEmpty() || readFile(dir + QStringLiteral("/name")) == hwmonName)
            return dir;
    }
    return QString();
}

struct DgpuReading {
    bool present = false;
    bool active = false;
    QString state;
    double tempC = 0;
    double utilPct = 0;
    double powerW = 0;
    double memoryMiB = 0;
};

} // namespace

class JunoSensorsPrivate
{
public:
    JunoSensorsPrivate(JunoSensorsPlugin *qq);

    void update();

    QString sysfs;
    QString procStat;
    QString dgpuPci;
    QString nvidiaSmi;

    KSysGuard::SensorContainer *container = nullptr;

    // cpu
    KSysGuard::SensorObject *cpuObject = nullptr;
    KSysGuard::SensorProperty *cpuTemp = nullptr;
    KSysGuard::SensorProperty *cpuBusy = nullptr;
    QString coretempDir;
    quint64 cpuIdlePrev = 0, cpuTotalPrev = 0;

    // fans
    KSysGuard::SensorObject *fanCpuObject = nullptr;
    KSysGuard::SensorObject *fanGpuObject = nullptr;
    KSysGuard::SensorProperty *fanCpuRpm = nullptr;
    KSysGuard::SensorProperty *fanCpuDuty = nullptr;
    KSysGuard::SensorProperty *fanGpuRpm = nullptr;
    KSysGuard::SensorProperty *fanGpuDuty = nullptr;
    QString clevofanDir;
    int fanCount = 0;

    // igpu
    KSysGuard::SensorObject *igpuObject = nullptr;
    KSysGuard::SensorProperty *igpuBusy = nullptr;
    KSysGuard::SensorProperty *igpuClock = nullptr;
    QString igpuGtDir;
    quint64 igpuRc6Prev = 0;
    QElapsedTimer igpuClockDelta;

    // dgpu
    KSysGuard::SensorObject *dgpuObject = nullptr;
    KSysGuard::SensorProperty *dgpuTemp = nullptr;
    KSysGuard::SensorProperty *dgpuBusy = nullptr;
    KSysGuard::SensorProperty *dgpuMemory = nullptr;
    KSysGuard::SensorProperty *dgpuPower = nullptr;
    KSysGuard::SensorProperty *dgpuState = nullptr;
    KSysGuard::SensorProperty *dgpuActiveGpu = nullptr;
    QElapsedTimer dgpuSmiClock;
    DgpuReading dgpuCached;

    // network
    KSysGuard::SensorObject *netObject = nullptr;
    KSysGuard::SensorProperty *netDown = nullptr;
    KSysGuard::SensorProperty *netUp = nullptr;
    QString netDir;
    quint64 netRxPrev = 0, netTxPrev = 0;
    QElapsedTimer netDelta;

    // power
    KSysGuard::SensorObject *powerObject = nullptr;
    KSysGuard::SensorProperty *powerSystem = nullptr;
    KSysGuard::SensorProperty *batteryCharge = nullptr;
    KSysGuard::SensorProperty *batteryTime = nullptr;
    QString raplDir;
    QString batteryDir;
    quint64 raplPrevUj = 0;
    QElapsedTimer raplDelta;
};

JunoSensorsPrivate::JunoSensorsPrivate(JunoSensorsPlugin *qq)
{
    sysfs = pathEnv("JUNO_KSS_SYSFS", QStringLiteral("/sys"));
    procStat = pathEnv("JUNO_KSS_PROC_STAT", QStringLiteral("/proc/stat"));
    dgpuPci = pathEnv("JUNO_KSS_DGPU_PCI", sysfs + QStringLiteral("/bus/pci/devices/0000:01:00.0"));
    nvidiaSmi = pathEnv("JUNO_KSS_NVIDIA_SMI", QStringLiteral("nvidia-smi"));

    container = new KSysGuard::SensorContainer(QStringLiteral("juno"), i18nc("@title", "Juno Laptop"), qq);

    // --- CPU: coretemp hwmon temperature + /proc/stat busy fraction ----------
    cpuObject = new KSysGuard::SensorObject(QStringLiteral("cpu"), i18nc("@title", "CPU"), container);
    cpuTemp = new KSysGuard::SensorProperty(QStringLiteral("temperature"), i18nc("@title", "CPU Package Temperature"), cpuObject);
    cpuTemp->setUnit(KSysGuard::UnitCelsius);
    cpuTemp->setMin(0);
    cpuTemp->setMax(110);
    cpuBusy = new KSysGuard::SensorProperty(QStringLiteral("usage"), i18nc("@title", "CPU Usage"), 0, cpuObject);
    cpuBusy->setUnit(KSysGuard::UnitPercent);
    cpuBusy->setMin(0);
    cpuBusy->setMax(100);
    coretempDir = findHwmon(sysfs + QStringLiteral("/devices/platform"), QStringLiteral("coretemp.0"), QStringLiteral("coretemp"));

    igpuClockDelta.start();
    netDelta.start();
    raplDelta.start();

    // --- Fans: clevofan hwmon. Missing fan hardware -> no fan objects --------
    clevofanDir = findHwmon(sysfs + QStringLiteral("/devices/platform"), QStringLiteral("clevofan"), QString());
    if (!clevofanDir.isEmpty() && QFileInfo::exists(clevofanDir + QStringLiteral("/fan1_input"))) {
        fanCpuObject = new KSysGuard::SensorObject(QStringLiteral("fan-cpu"), i18nc("@title", "CPU Fan"), container);
        fanCpuRpm = new KSysGuard::SensorProperty(QStringLiteral("rpm"), i18nc("@title", "CPU Fan Speed"), 0, fanCpuObject);
        fanCpuRpm->setUnit(KSysGuard::UnitRpm);
        fanCpuRpm->setMin(0);
        fanCpuRpm->setMax(6000);
        fanCpuDuty = new KSysGuard::SensorProperty(QStringLiteral("duty"), i18nc("@title", "CPU Fan Duty"), 0, fanCpuObject);
        fanCpuDuty->setUnit(KSysGuard::UnitPercent);
        fanCpuDuty->setMin(0);
        fanCpuDuty->setMax(100);
        fanCount = 1;
        if (QFileInfo::exists(clevofanDir + QStringLiteral("/fan2_input"))) {
            fanGpuObject = new KSysGuard::SensorObject(QStringLiteral("fan-gpu"), i18nc("@title", "GPU Fan"), container);
            fanGpuRpm = new KSysGuard::SensorProperty(QStringLiteral("rpm"), i18nc("@title", "GPU Fan Speed"), 0, fanGpuObject);
            fanGpuRpm->setUnit(KSysGuard::UnitRpm);
            fanGpuRpm->setMin(0);
            fanGpuRpm->setMax(6000);
            fanGpuDuty = new KSysGuard::SensorProperty(QStringLiteral("duty"), i18nc("@title", "GPU Fan Duty"), 0, fanGpuObject);
            fanGpuDuty->setUnit(KSysGuard::UnitPercent);
            fanGpuDuty->setMin(0);
            fanGpuDuty->setMax(100);
            fanCount = 2;
        }
    }

    // --- Intel iGPU: RC6 residency + render clock ----------------------------
    // The i915 PMU needs CAP_PERFMON, so rc6_survey files are what's readable.
    for (const char *card : {"card0", "card1"}) {
        const QString gt = sysfs + QStringLiteral("/class/drm/") + QLatin1String(card) + QStringLiteral("/gt/gt0");
        if (QFileInfo::exists(gt + QStringLiteral("/rc6_residency_ms"))) {
            igpuGtDir = gt;
            break;
        }
    }
    if (!igpuGtDir.isEmpty()) {
        igpuObject = new KSysGuard::SensorObject(QStringLiteral("igpu"), i18nc("@title", "Integrated GPU"), container);
        igpuBusy = new KSysGuard::SensorProperty(QStringLiteral("usage"), i18nc("@title", "iGPU Usage"), 0, igpuObject);
        igpuBusy->setUnit(KSysGuard::UnitPercent);
        igpuBusy->setMin(0);
        igpuBusy->setMax(100);
        igpuClock = new KSysGuard::SensorProperty(QStringLiteral("frequency"), i18nc("@title", "iGPU Render Clock"), 0, igpuObject);
        igpuClock->setUnit(KSysGuard::UnitMegaHertz);
    }

    // --- dGPU: present/active state first; never wake a suspended card -------
    if (QFileInfo::exists(dgpuPci)) {
        dgpuObject = new KSysGuard::SensorObject(QStringLiteral("dgpu"), i18nc("@title", "Discrete GPU (NVIDIA)"), container);
        dgpuTemp = new KSysGuard::SensorProperty(QStringLiteral("temperature"), i18nc("@title", "GPU Temperature"), dgpuObject);
        dgpuTemp->setUnit(KSysGuard::UnitCelsius);
        dgpuTemp->setMin(0);
        dgpuTemp->setMax(110);
        dgpuBusy = new KSysGuard::SensorProperty(QStringLiteral("usage"), i18nc("@title", "GPU Usage"), 0, dgpuObject);
        dgpuBusy->setUnit(KSysGuard::UnitPercent);
        dgpuBusy->setMin(0);
        dgpuBusy->setMax(100);
        dgpuMemory = new KSysGuard::SensorProperty(QStringLiteral("memory"), i18nc("@title", "GPU Memory Used"), 0, dgpuObject);
        dgpuMemory->setUnit(KSysGuard::UnitMegaByte);
        dgpuPower = new KSysGuard::SensorProperty(QStringLiteral("power"), i18nc("@title", "GPU Power"), 0.0, dgpuObject);
        dgpuPower->setUnit(KSysGuard::UnitWatt);
        dgpuState = new KSysGuard::SensorProperty(QStringLiteral("state"), i18nc("@title", "GPU Power State"), i18nc("@info", "absent"), dgpuObject);
        // The which-GPU-runs answer, surfaced as its own sensor for panel widgets.
        dgpuActiveGpu = new KSysGuard::SensorProperty(QStringLiteral("activeGpu"), i18nc("@title", "GPU In Use"), i18nc("@info", "iGPU"), dgpuObject);
    }

    // --- Network: sum over physical interfaces (a `device` link marks them) --
    netDir = sysfs + QStringLiteral("/class/net");
    netObject = new KSysGuard::SensorObject(QStringLiteral("network"), i18nc("@title", "Network"), container);
    netDown = new KSysGuard::SensorProperty(QStringLiteral("download"), i18nc("@title", "Download Rate"), 0.0, netObject);
    netDown->setUnit(KSysGuard::UnitByteRate);
    netUp = new KSysGuard::SensorProperty(QStringLiteral("upload"), i18nc("@title", "Upload Rate"), 0.0, netObject);
    netUp->setUnit(KSysGuard::UnitByteRate);

    // --- Power/battery --------------------------------------------------------
    const QString raplBase = sysfs + QStringLiteral("/class/powercap/intel-rapl");
    const QString raplName = readFile(raplBase + QStringLiteral("/intel-rapl:0/name"));
    if (raplName == QLatin1String("psys") || raplName == QLatin1String("package-0"))
        raplDir = raplBase + QStringLiteral("/intel-rapl:0");
    for (const QString &bat : QDir(sysfs + QStringLiteral("/class/power_supply")).entryList(QDir::Dirs | QDir::NoDotAndDotDot)) {
        if (readFile(sysfs + QStringLiteral("/class/power_supply/") + bat + QStringLiteral("/type")) == QLatin1String("Battery")) {
            batteryDir = sysfs + QStringLiteral("/class/power_supply/") + bat;
            break;
        }
    }
    powerObject = new KSysGuard::SensorObject(QStringLiteral("power"), i18nc("@title", "Power"), container);
    powerSystem = new KSysGuard::SensorProperty(QStringLiteral("system"), i18nc("@title", "System Power Draw"), 0.0, powerObject);
    powerSystem->setUnit(KSysGuard::UnitWatt);
    if (!batteryDir.isEmpty()) {
        batteryCharge = new KSysGuard::SensorProperty(QStringLiteral("batteryPercentage"), i18nc("@title", "Battery Charge"), 0, powerObject);
        batteryCharge->setUnit(KSysGuard::UnitPercent);
        batteryCharge->setMin(0);
        batteryCharge->setMax(100);
        batteryTime = new KSysGuard::SensorProperty(QStringLiteral("batteryTime"), i18nc("@title", "Battery Time"), 0, powerObject);
        batteryTime->setUnit(KSysGuard::UnitTime);
    }
}

void JunoSensorsPrivate::update()
{
    bool ok = false;

    // CPU
    if (!coretempDir.isEmpty()) {
        const double micro = readNumber(coretempDir + QStringLiteral("/temp1_input"), ok);
        if (ok)
            cpuTemp->setValue(micro / 1000.0);
    }
    const QStringList stat = readFile(procStat).split(QRegularExpression(QStringLiteral("\\s+")), Qt::SkipEmptyParts);
    if (stat.size() >= 5 && stat[0] == QLatin1String("cpu")) {
        bool okStat = true;
        quint64 idle = stat[4].toULongLong(&okStat);
        quint64 total = 0;
        for (int i = 1; i < stat.size(); i++)
            total += stat[i].toULongLong(&okStat);
        if (okStat && cpuTotalPrev > 0 && total > cpuTotalPrev) {
            const double busy = 100.0 * (total - cpuTotalPrev - (idle - cpuIdlePrev)) / (total - cpuTotalPrev);
            cpuBusy->setValue(qBound(0.0, busy, 100.0));
        }
        cpuIdlePrev = idle;
        cpuTotalPrev = total;
    }

    // Fans
    if (fanCpuObject) {
        const double rpm = readNumber(clevofanDir + QStringLiteral("/fan1_input"), ok);
        if (ok)
            fanCpuRpm->setValue(rpm);
        const double pwm = readNumber(clevofanDir + QStringLiteral("/pwm1"), ok);
        if (ok)
            fanCpuDuty->setValue(pwm * 100.0 / 255.0);
    }
    if (fanGpuObject) {
        const double rpm = readNumber(clevofanDir + QStringLiteral("/fan2_input"), ok);
        if (ok)
            fanGpuRpm->setValue(rpm);
        const double pwm = readNumber(clevofanDir + QStringLiteral("/pwm2"), ok);
        if (ok)
            fanGpuDuty->setValue(pwm * 100.0 / 255.0);
    }

    // iGPU: busy = 1 - drc6/dwall (the only unprivileged signal; see tray notes)
    if (igpuObject) {
        const double rc6ms = readNumber(igpuGtDir + QStringLiteral("/rc6_residency_ms"), ok);
        const bool okRc6 = ok;
        const qint64 wall = igpuClockDelta.restart();
        if (okRc6 && wall > 0 && igpuRc6Prev > 0) {
            const double frac = 1.0 - (rc6ms - igpuRc6Prev) / wall;
            igpuBusy->setValue(qBound(0.0, frac * 100.0, 100.0));
        }
        if (okRc6)
            igpuRc6Prev = rc6ms;
        const double mhz = readNumber(igpuGtDir + QStringLiteral("/rps_act_freq_mhz"), ok);
        if (ok)
            igpuClock->setValue(mhz);
    }

    // dGPU: power state BEFORE any query; a suspended card answers cold/idle
    // and is never asked (nvidia-smi would wake it).
    if (dgpuObject) {
        DgpuReading r;
        r.present = true;
        const QString runtime = readFile(dgpuPci + QStringLiteral("/power/runtime_status"));
        const QString powerState = readFile(dgpuPci + QStringLiteral("/power_state"));
        r.active = (runtime == QLatin1String("active"));
        r.state = r.active ? i18nc("@info GPU state", "active") : powerState.isEmpty() ? runtime : powerState;
        if (r.active) {
            // nvidia-smi at most once every 2 s even though update() fires ~2 Hz.
            if (!dgpuSmiClock.isValid() || dgpuSmiClock.elapsed() >= 2000) {
                QProcess p;
                p.start(nvidiaSmi,
                        {QStringLiteral("--query-gpu=temperature.gpu,utilization.gpu,power.draw,memory.used"),
                         QStringLiteral("--format=csv,noheader,nounits")});
                p.waitForFinished(2000);
                const QString out = QString::fromLocal8Bit(p.readAllStandardOutput()).trimmed();
                if (p.exitStatus() == QProcess::NormalExit && !out.isEmpty()) {
                    const QStringList f = out.split(QLatin1Char(','));
                    if (f.size() >= 4) {
                        dgpuCached.tempC = f[0].trimmed().toDouble();
                        dgpuCached.utilPct = f[1].trimmed().toDouble();
                        dgpuCached.powerW = f[2].trimmed().toDouble();
                        dgpuCached.memoryMiB = f[3].trimmed().toDouble();
                        dgpuCached.present = true;
                        dgpuCached.active = true;
                    }
                }
                dgpuSmiClock.start();
            }
            dgpuTemp->setValue(dgpuCached.tempC);
            dgpuBusy->setValue(dgpuCached.utilPct);
            dgpuMemory->setValue(dgpuCached.memoryMiB);
            dgpuPower->setValue(dgpuCached.powerW);
        } else {
            // Suspended: the state text replaces the invented temperature.
            dgpuCached = DgpuReading{};
            dgpuCached.present = true;
            dgpuSmiClock.invalidate();
        }
        dgpuState->setValue(r.state.isEmpty() ? (r.active ? i18nc("@info", "active") : i18nc("@info", "suspended")) : r.state);
        dgpuActiveGpu->setValue(r.active ? i18nc("@info", "dGPU") : i18nc("@info", "iGPU"));
    }

    // Network
    {
        quint64 rx = 0, tx = 0;
        const QDir n(netDir);
        for (const QString &iface : n.entryList(QDir::Dirs | QDir::NoDotAndDotDot)) {
            const QString base = n.filePath(iface);
            if (!QFileInfo::exists(base + QStringLiteral("/device")))
                continue; // lo, bridges, veth, vpn tunnels: not physical
            bool okR = false, okT = false;
            rx += readNumber(base + QStringLiteral("/statistics/rx_bytes"), okR);
            tx += readNumber(base + QStringLiteral("/statistics/tx_bytes"), okT);
        }
        const qint64 wall = netDelta.restart();
        if (wall > 0 && netRxPrev > 0) {
            netDown->setValue((rx - netRxPrev) * 1000.0 / wall);
            netUp->setValue((tx - netTxPrev) * 1000.0 / wall);
        }
        netRxPrev = rx;
        netTxPrev = tx;
    }

    // Power: RAPL psys preferred (whole platform), battery as fallback.
    if (powerObject) {
        if (!raplDir.isEmpty()) {
            const quint64 uj = readNumber(raplDir + QStringLiteral("/energy_uj"), ok);
            const bool okUj = ok;
            const qint64 wall = okUj ? raplDelta.restart() : 0;
            if (okUj && wall > 0 && uj >= raplPrevUj && raplPrevUj > 0) {
                const double w = (uj - raplPrevUj) * 1e-6 / (wall * 1e-3);
                powerSystem->setValue(w);
            }
            if (okUj)
                raplPrevUj = uj;
        } else if (!batteryDir.isEmpty()) {
            const double powerUw = readNumber(batteryDir + QStringLiteral("/power_now"), ok);
            if (ok && readFile(batteryDir + QStringLiteral("/status")) == QLatin1String("Discharging"))
                powerSystem->setValue(powerUw * 1e-6);
        }
        if (batteryCharge) {
            const double pct = readNumber(batteryDir + QStringLiteral("/capacity"), ok);
            if (ok) {
                batteryCharge->setValue(pct);
            } else {
                // capacity file absent on some drivers: energy_now/energy_full
                const double now_ = readNumber(batteryDir + QStringLiteral("/energy_now"), ok);
                const double full = ok ? readNumber(batteryDir + QStringLiteral("/energy_full"), ok) : 0;
                if (ok && full > 0)
                    batteryCharge->setValue(100.0 * now_ / full);
            }
            const double en = readNumber(batteryDir + QStringLiteral("/energy_now"), ok);
            const double pw = ok ? readNumber(batteryDir + QStringLiteral("/power_now"), ok) : 0;
            if (ok && pw > 0 && batteryTime) {
                const bool discharge = readFile(batteryDir + QStringLiteral("/status")) == QLatin1String("Discharging");
                batteryTime->setValue(discharge
                                          ? QVariant::fromValue<qlonglong>(qlonglong(en / pw * 3600.0))
                                          : QVariant());
            }
        }
    }
}

JunoSensorsPlugin::JunoSensorsPlugin(QObject *parent, const QVariantList &args)
    : SensorPlugin(parent, args)
{
    d = std::make_unique<JunoSensorsPrivate>(this);
}

JunoSensorsPlugin::~JunoSensorsPlugin() = default;

void JunoSensorsPlugin::update()
{
    d->update();
}

K_PLUGIN_CLASS_WITH_JSON(JunoSensorsPlugin, "metadata.json")

#include "junoplugin.moc"
#include "moc_junoplugin.cpp"
