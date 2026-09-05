// SPDX-License-Identifier: GPL-2.0-or-later
// SPDX-FileCopyrightText: 2026 Marco Barbone
#pragma once

#include <memory>

#include <systemstats/SensorPlugin.h>

class JunoSensorsPrivate;

class JunoSensorsPlugin : public KSysGuard::SensorPlugin
{
    Q_OBJECT
public:
    JunoSensorsPlugin(QObject *parent, const QVariantList &args);
    ~JunoSensorsPlugin() override;

    QString providerName() const override
    {
        return QStringLiteral("juno-laptop");
    }

    void update() override;

private:
    std::unique_ptr<JunoSensorsPrivate> d;
};
