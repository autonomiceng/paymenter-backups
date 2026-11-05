{{-- Re-localize after Livewire updates, as with individual backup timestamps. --}}
<span wire:replace.self title="{{ $fallback }}" data-frequency="{{ $frequency }}" data-instants="{{ json_encode($instants) }}" data-every="{{ $every }}" x-data x-init="
    const dates = JSON.parse($el.dataset.instants).map(value => new Date(value));
    const clock = new Intl.DateTimeFormat('en-US', {hour: 'numeric', minute: '2-digit', timeZoneName: 'short'});
    const weekday = new Intl.DateTimeFormat('en-US', {weekday: 'long'});
    if (dates.length) {
        if ($el.dataset.frequency === 'weekly') $el.textContent = 'Weekly, ' + dates.map(date => weekday.format(date) + ' at ' + clock.format(date)).join(', ');
        else if ($el.dataset.frequency === 'daily') $el.textContent = 'Daily at ' + clock.format(dates[0]);
        else {
            const parts = new Intl.DateTimeFormat('en-US', {minute: '2-digit', timeZoneName: 'short'}).formatToParts(dates[0]);
            const minute = parts.find(part => part.type === 'minute').value.padStart(2, '0');
            const zone = parts.find(part => part.type === 'timeZoneName').value;
            $el.textContent = ($el.dataset.every === '1' ? 'Every hour' : 'Every ' + $el.dataset.every + ' hours') + ' at :' + minute + ' ' + zone;
        }
    }
">{{ $fallback }}</span>
