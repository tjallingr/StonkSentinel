# Per-collector schedules

`finoverview-collect@.timer` ships a 4-hourly default. Override per instance with
drop-ins, so each source runs as often as it's worth running.

    sudo mkdir -p /etc/systemd/system/finoverview-collect@saxo.timer.d
    sudo cp saxo.conf /etc/systemd/system/finoverview-collect@saxo.timer.d/override.conf

Repeat for the others, then:

    sudo systemctl daemon-reload
    sudo systemctl enable --now finoverview-collect@{fx,manual,enablebanking,saxo}.timer
    systemctl list-timers 'finoverview*'
