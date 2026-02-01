#!/usr/bin/perl -w
#
# mysql-explain-bargraph.pl - Generate horizontal bar chart SVG showing self-time per operation
#
use strict;
use JSON::PP;
use Getopt::Long;

sub xml_escape {
    my ($str) = @_;
    return '' unless defined $str;
    $str =~ s/&/&amp;/g;
    $str =~ s/</&lt;/g;
    $str =~ s/>/&gt;/g;
    $str =~ s/"/&quot;/g;
    return $str;
}

my $width = 1200;
my $title = "MySQL Query Performance";
my $help = 0;

GetOptions(
    'width=i' => \$width,
    'title=s' => \$title,
    'help'    => \$help,
) or die "Usage: $0 [--width N] [--title TEXT] input.json > output.svg\n";

if ($help) {
    print "Usage: $0 [--width N] [--title TEXT] input.json > output.svg\n";
    exit 0;
}

my $json_text = do { local $/; <> };
$json_text =~ s/^.*?EXPLAIN:\s*//s;
my $data = decode_json($json_text);

my @operations = ();

sub process_node {
    my ($node, $depth) = @_;
    return unless ref $node eq 'HASH';

    my $op = $node->{operation} // 'unknown';
    my $table = $node->{table_name} // '';
    my $index = $node->{index_name} // '';
    my $alias = $node->{alias} // '';
    my $rows = $node->{actual_rows} // 0;
    my $loops = $node->{actual_loops} // 1;
    my $time_ms = ($node->{actual_last_row_ms} // 0) * $loops;
    my $condition = $node->{condition} // '';

    # Calculate children time
    my $children_time = 0;
    my @children = ();
    if (exists $node->{inputs} && ref $node->{inputs} eq 'ARRAY') {
        @children = @{$node->{inputs}};
    }

    foreach my $child (@children) {
        next unless ref $child eq 'HASH';
        my $child_time = ($child->{actual_last_row_ms} // 0) * ($child->{actual_loops} // 1);
        $children_time += $child_time;
        process_node($child, $depth + 1);
    }

    my $self_time = $time_ms - $children_time;
    $self_time = 0 if $self_time < 0;

    # Clean up operation name
    $op =~ s/`//g;
    $op =~ s/DATE'(\d{4}-\d{2}-\d{2})'/$1/g;  # DATE'9999-01-01' -> 9999-01-01
    
    # Build label
    my $label = $op;
    if ($table && $table !~ /temporary/) {
        $label .= " [$table" . ($index ? ".$index" : "") . "]";
    }
    if ($loops > 1) {
        $label .= " x$loops";
    }

    push @operations, {
        label => $label,
        self_time => $self_time,
        total_time => $time_ms,
        rows => $rows,
        loops => $loops,
        depth => $depth,
    };
}

process_node($data, 0);

# Sort by self_time descending
@operations = sort { $b->{self_time} <=> $a->{self_time} } @operations;

# Filter out zero-time operations (keep if >= 0.001ms for fast queries)
@operations = grep { $_->{self_time} >= 0.001 } @operations;

# Calculate total time
my $total_time = 0;
$total_time += $_->{self_time} for @operations;
$total_time = 0.001 if $total_time == 0;

# Determine if we should use microseconds (for sub-ms queries)
my $use_microseconds = ($total_time > 0 && $total_time < 1);
my $unit = $use_microseconds ? "µs" : "ms";
my $multiplier = $use_microseconds ? 1000 : 1;

# Convert times if using microseconds
if ($use_microseconds) {
    $_->{self_time} *= $multiplier for @operations;
    $_->{total_time} *= $multiplier for @operations;
    $total_time *= $multiplier;
}

# SVG dimensions
my $bar_height = 32;
my $bar_gap = 8;
my $left_margin = 10;
my $right_margin = 10;
my $top_margin = 60;
my $bottom_margin = 40;
my $label_width = 500;
my $bar_area_width = $width - $left_margin - $right_margin - $label_width - 150;

my $num_bars = scalar @operations;
my $height = $top_margin + ($num_bars * ($bar_height + $bar_gap)) + $bottom_margin;

# Color palette (warm colors)
my @colors = (
    'rgb(255,90,90)',    # red
    'rgb(255,130,70)',   # orange-red
    'rgb(255,165,50)',   # orange
    'rgb(255,200,50)',   # yellow-orange
    'rgb(255,220,80)',   # yellow
    'rgb(200,200,100)',  # olive
    'rgb(150,200,150)',  # light green
    'rgb(100,180,180)',  # teal
);

# Start SVG
print qq{<?xml version="1.0" standalone="no"?>
<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">
<svg version="1.1" width="$width" height="$height" xmlns="http://www.w3.org/2000/svg">
<style>
  text { font-family: Arial, sans-serif; font-size: 13px; }
  .title { font-size: 18px; font-weight: bold; }
  .label { font-size: 12px; }
  .value { font-size: 12px; font-weight: bold; }
  .bar:hover { opacity: 0.8; cursor: pointer; }
  .header { font-size: 11px; fill: #666; }
</style>
<rect width="100%" height="100%" fill="#fafafa"/>
<text x="${\($width/2)}" y="30" text-anchor="middle" class="title">$title</text>
<text x="${\($width/2)}" y="48" text-anchor="middle" class="header">Self-time per operation (sorted by time, slowest first) | Total: ${\($total_time >= 1 ? sprintf("%.0f", $total_time) : sprintf("%.3f", $total_time))} $unit</text>
};

# Draw bars
my $y = $top_margin;
my $i = 0;

foreach my $op (@operations) {
    my $pct = ($op->{self_time} / $total_time) * 100;
    my $bar_width = ($op->{self_time} / $total_time) * $bar_area_width;
    $bar_width = 3 if $bar_width < 3 && $op->{self_time} > 0;  # minimum visible width

    my $color = $colors[$i % scalar(@colors)];
    my $label = $op->{label};
    $label = substr($label, 0, 85) . "..." if length($label) > 88;

    my $bar_x = $left_margin + $label_width;
    my $text_y = $y + ($bar_height / 2) + 4;

    # Label (escaped for XML)
    my $label_escaped = xml_escape($label);
    print qq{<text x="${\($left_margin + $label_width - 10)}" y="$text_y" text-anchor="end" class="label">$label_escaped</text>\n};

    # Bar
    my $title_label_escaped = xml_escape($op->{label});
    print qq{<rect class="bar" x="$bar_x" y="$y" width="$bar_width" height="$bar_height" fill="$color" rx="3" ry="3">};
    print qq{<title>$title_label_escaped\nSelf-time: ${\($op->{self_time} >= 1 ? sprintf("%.0f", $op->{self_time}) : sprintf("%.3f", $op->{self_time}))} $unit (${\(sprintf "%.1f", $pct)}%)\nRows: ${\(sprintf "%.0f", $op->{rows})}\nLoops: $op->{loops}</title>};
    print qq{</rect>\n};

    # Value label (show decimals for fast queries)
    my $value_x = $bar_x + $bar_width + 8;
    my $value_text;
    if ($op->{self_time} >= 1) {
        $value_text = sprintf("%.0f $unit (%.1f%%)", $op->{self_time}, $pct);
    } else {
        $value_text = sprintf("%.3f $unit (%.1f%%)", $op->{self_time}, $pct);
    }
    print qq{<text x="$value_x" y="$text_y" class="value">$value_text</text>\n};

    $y += $bar_height + $bar_gap;
    $i++;
}

print "</svg>\n";
