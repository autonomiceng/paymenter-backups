{{-- One marked step: its name, a state word on the right, then one line of detail. --}}
<div class="domains-trace-line">
    <span class="domains-trace-title">{{ $label }}</span>
    @if (filled($value ?? null)) <span class="domains-trace-word">{{ $value }}</span> @endif
</div>
@if (filled($detail ?? null))
    <div class="domains-trace-detail">{{ $detail }}</div>
@endif
