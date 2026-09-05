// SPDX-License-Identifier: GPL-2.0-or-later
// Test probe: load the sensor plugin directly (no daemon, no dbus), tick
// update() a few times, and print every sensor value. The gate script asserts
// on this dump against fixture expectations.
#include <QCoreApplication>
#include <QDebug>
#include <QElapsedTimer>
#include <QPluginLoader>
#include <QTextStream>
#include <QThread>

#include <KPluginFactory>

#include <systemstats/SensorContainer.h>
#include <systemstats/SensorObject.h>
#include <systemstats/SensorPlugin.h>
#include <systemstats/SensorProperty.h>

int main(int argc, char **argv)
{
    QCoreApplication app(argc, argv);
    if (argc < 2) {
        qFatal("usage: probe PATH-TO-kystemstats_plugin_juno.so [ticks]");
    }
    const QString libPath = QString::fromLocal8Bit(argv[1]);
    const int ticks = argc > 2 ? atoi(argv[2]) : 4;

    QPluginLoader loader(libPath);
    if (!loader.load()) {
        qFatal("cannot load %s: %s", qPrintable(libPath), qPrintable(loader.errorString()));
    }
    const KPluginMetaData md(loader);
    auto result = KPluginFactory::instantiatePlugin<KSysGuard::SensorPlugin>(md);
    if (!result)
        qFatal("instantiate failed: %s", qPrintable(result.errorString));
    auto *plugin = result.plugin;

    QElapsedTimer tickClock;
    tickClock.start();
    auto findProp = [plugin](const QString &path) -> KSysGuard::SensorProperty * {
        const QStringList parts = path.split(QLatin1Char('/'));
        for (auto *container : plugin->containers())
            if (container->id() == parts[0])
                if (auto *object = container->object(parts[1]))
                    return object->sensor(parts[2]);
        return nullptr;
    };
    auto *power = findProp(QStringLiteral("juno/power/system"));
    for (int i = 0; i < ticks; ++i) {
        QThread::msleep(650);      // delta-based sensors need real time between ticks
        plugin->update();
        app.processEvents();
        QTextStream out(stdout);
        out << "tick " << i << " at " << tickClock.elapsed() << " ms";
        for (auto *container : plugin->containers())
            for (auto *object : container->objects())
                for (auto *prop : object->sensors())
                    if (prop->id() == QStringLiteral("system"))
                        out << " powerW=" << prop->value().toString();
        out << "\n";
    }

    for (auto *container : plugin->containers()) {
        for (auto *object : container->objects()) {
            for (auto *prop : object->sensors()) {
                const QVariant v = prop->value();
                QTextStream out(stdout);
                out << container->id() << u'/' << object->id() << u'/' << prop->id()
                    << " = " << (v.isValid() ? v.toString() : QStringLiteral("unset")) << '\n';
            }
        }
    }
    return 0;
}
