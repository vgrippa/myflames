#!/usr/bin/perl -w
#
# mysql-explain.pl - Unified command: flame graph, bar chart, or treemap from MySQL EXPLAIN ANALYZE JSON
#
# Single parser for all output types. Usage:
#   ./mysql-explain.pl [options] explain.json > output.svg
#   ./mysql-explain.pl --type bargraph explain.json > query-bar.svg
#   ./mysql-explain.pl --type treemap explain.json > query-treemap.svg
#
use strict;
use JSON::PP;
use Getopt::Long;
use File::Basename;
use File::Spec;
use IPC::Open2;

#-------------------------------------------------------------------------------
# Options
#-------------------------------------------------------------------------------
my $type = "flamegraph";
my $width = 1800;
my $height = 32;
my $colors = "hot";
my $title = "MySQL Query Plan";
my $enhance_tooltips = 1;
my $inverted = 0;
my $help = 0;

GetOptions(
    'type=s'     => \$type,
    'width=i'    => \$width,
    'height=i'   => \$height,
    'colors=s'   => \$colors,
    'title=s'    => \$title,
    'enhance!'   => \$enhance_tooltips,
    'inverted'   => \$inverted,
    'help'       => \$help,
) or die "Error in command line arguments\n";

$type = lc($type);
unless ($type eq 'flamegraph' || $type eq 'bargraph' || $type eq 'treemap') {
    die "Invalid --type: must be 'flamegraph', 'bargraph', or 'treemap' (got '$type')\n";
}

# Type-specific defaults
$width = 1200 if $type eq 'bargraph';
$width = 1200 if $type eq 'treemap';

if ($help) {
    print <<'USAGE';
Usage: mysql-explain.pl [--type flamegraph|bargraph|treemap] [options] explain.json > output.svg

  Default: flame graph. Use --type bargraph for bar chart, --type treemap for treemap.
  All types use the same parser and JSON format.

Options:
  --type TYPE     Output: flamegraph (default), bargraph, or treemap
  --width N       SVG width (default: 1800 flamegraph, 1200 bargraph/treemap)
  --height N      Frame height for flame graph (default: 32)
  --colors SCHEME Color scheme for flame graph: hot, mem, io, etc. (default: hot)
  --title TEXT    Title (default: "MySQL Query Plan")
  --inverted      Flame graph only: icicle (inverted)
  --enhance / --no-enhance   Flame graph: detailed tooltips (default: on)
  --help          Show this help

Examples:
  ./mysql-explain.pl explain.json > query.svg
  ./mysql-explain.pl --type bargraph explain.json > query-bar.svg
  ./mysql-explain.pl --type treemap explain.json > query-treemap.svg
USAGE
    exit 0;
}

#-------------------------------------------------------------------------------
# Read input
#-------------------------------------------------------------------------------
my $json_text = do { local $/; <> };
$json_text =~ s/^.*?EXPLAIN:\s*//s;
my $data = decode_json($json_text);

#-------------------------------------------------------------------------------
# Unified parser: one tree structure for all output types
# Each node: { short_label, folded_label, details, self_time, total_time, rows, loops, children }
#-------------------------------------------------------------------------------

sub xml_escape {
    my ($str) = @_;
    return '' unless defined $str;
    $str =~ s/&/&amp;/g;
    $str =~ s/</&lt;/g;
    $str =~ s/>/&gt;/g;
    $str =~ s/"/&quot;/g;
    return $str;
}

sub enhance_tooltip_flame {
    my ($original, $op_details) = @_;
    my $best = undef;
    my $best_score = 0;
    for my $label (keys %$op_details) {
        my $d = $op_details->{$label};
        my $score = 0;
        $score += 20 if $original =~ /\Q$label\E/i;
        $score += 10 if $d->{index_name} && $original =~ /\.\Q$d->{index_name}\E\]|using\s+\Q$d->{index_name}\E/i;
        $score += 3 if $d->{table_name} && $original =~ /\Q$d->{table_name}\E/i;
        $score += 5 if defined $d->{actual_rows} && $original =~ /rows[=:]?\s*(\d+)\b/i && int($d->{actual_rows} + 0.5) == $1;
        $score += 3 if $original =~ /starts[=:]?\s*(\d+)\b/i && ($d->{actual_loops} // 1) == $1;
        ($best, $best_score) = ($d, $score) if $score > $best_score;
    }
    return $original unless $best_score >= 5 && $best;
    my @lines = ($original, "");
    push @lines, "Table: " . ($best->{schema_name} ? xml_escape($best->{schema_name}) . "." : "") . xml_escape($best->{table_name}) . ($best->{index_name} ? " (index: " . xml_escape($best->{index_name}) . ")" : "") if $best->{table_name};
    push @lines, "Access: $best->{access_type}" if $best->{access_type};
    if (defined $best->{actual_rows}) {
        my $ri = sprintf("Rows: %.0f actual", $best->{actual_rows});
        $ri .= sprintf(" (%.0f estimated)", $best->{estimated_rows}) if defined $best->{estimated_rows};
        my $est = $best->{estimated_rows} // 0;
        my $r = ($est > 0) ? ($best->{actual_rows} / $est) : 0;
        $ri .= " [UNDERESTIMATE]" if $r > 2;
        $ri .= " [OVERESTIMATE]" if $r < 0.5 && $r > 0;
        push @lines, $ri;
    }
    push @lines, "Loops: $best->{actual_loops}" if $best->{actual_loops} && $best->{actual_loops} > 1;
    push @lines, sprintf("Time: %.3f ms (last row)", $best->{actual_last_row_ms}) if defined $best->{actual_last_row_ms};
    push @lines, sprintf("Cost: %.2f", $best->{estimated_total_cost}) if defined $best->{estimated_total_cost};
    my $cond = xml_escape($best->{condition});
    $cond = substr($cond, 0, 80) . "..." if length($cond) > 83;
    push @lines, "Condition: $cond" if $best->{condition};
    push @lines, "Ranges: " . xml_escape(join(", ", @{$best->{ranges}})) if $best->{ranges} && @{$best->{ranges}};
    push @lines, "Covering: " . ($best->{covering} ? "Yes" : "No") if defined $best->{covering};
    return join("&#10;", @lines);
}

sub build_short_label {
    my ($op, $table, $index, $condition) = @_;
    $table //= '';
    $index //= '';
    $condition //= '';
    my $label;
    if ($op =~ /^Table scan/i) {
        $label = "Table scan" . ($table ? " [$table]" : "");
    } elsif ($op =~ /^Index range scan/i) {
        $label = "Index range scan" . ($table && $index ? " [$table.$index]" : "");
    } elsif ($op =~ /^Index scan/i) {
        $label = "Index scan" . ($table && $index ? " [$table.$index]" : "");
    } elsif ($op =~ /^Index lookup/i) {
        $label = "Index lookup" . ($table && $index ? " [$table.$index]" : "");
    } elsif ($op =~ /^Single-row index lookup/i) {
        $label = "Single-row lookup" . ($table && $index ? " [$table.$index]" : "");
    } elsif ($op =~ /^Covering index/i) {
        $label = "Covering index" . ($table && $index ? " [$table.$index]" : "");
    } elsif ($op =~ /^Filter/i) {
        my $cond = $condition;
        $cond =~ s/`//g;
        $cond = substr($cond, 0, 40) . "..." if length($cond) > 43;
        $label = "Filter: ($cond)";
    } elsif ($op =~ /^Sort/i) {
        $label = ($op =~ /limit input to (\d+)/i) ? "Sort (limit $1)" : "Sort";
    } elsif ($op =~ /^Nested loop/i) {
        if ($op =~ /inner/i) { $label = "Nested loop inner join"; }
        elsif ($op =~ /left/i) { $label = "Nested loop left join"; }
        elsif ($op =~ /semi/i) { $label = "Nested loop semi join"; }
        else { $label = "Nested loop join"; }
    } elsif ($op =~ /^Aggregate/i) { $label = "Aggregate"; }
    elsif ($op =~ /^Group/i) { $label = "Group"; }
    elsif ($op =~ /^Materialize/i) { $label = "Materialize"; }
    elsif ($op =~ /^Stream results/i) { $label = "Stream results"; }
    elsif ($op =~ /^Limit/i) {
        $label = ($op =~ /(\d+) row/i) ? "Limit: $1 rows" : "Limit";
    } elsif ($op =~ /^Intersect/i) { $label = "Intersect (row ID)"; }
    elsif ($op =~ /^Union/i) { $label = "Union"; }
    else {
        $label = $op;
        $label = substr($label, 0, 50) . "..." if length($label) > 53;
    }
    return $label;
}

sub build_folded_label {
    my ($node) = @_;
    my $op = $node->{operation} // 'unknown';
    my $table = $node->{table_name} // '';
    my $index = $node->{index_name} // '';
    my $rows = $node->{actual_rows};
    my $loops = $node->{actual_loops} // 1;
    $op =~ s/`//g;
    my $label;
    if ($op =~ /^Table scan/i) { $label = "TABLE SCAN [$table]"; }
    elsif ($op =~ /^Index range scan/i) { $label = "INDEX RANGE SCAN [$table.$index]"; }
    elsif ($op =~ /^Index scan/i) { $label = "INDEX SCAN [$table.$index]"; }
    elsif ($op =~ /^Index lookup/i) { $label = "INDEX LOOKUP [$table.$index]"; }
    elsif ($op =~ /^Single-row index lookup/i) { $label = "SINGLE ROW LOOKUP [$table.$index]"; }
    elsif ($op =~ /^Covering index/i) { $label = "COVERING INDEX [$table.$index]"; }
    elsif ($op =~ /^Filter/i) {
        my $cond = $node->{condition} // '';
        $cond =~ s/`//g;
        $cond = substr($cond, 0, 50) . ".." if length($cond) > 52;
        $label = "FILTER ($cond)";
    }
    elsif ($op =~ /^Sort/i) {
        $label = "SORT";
        $label .= " (row IDs)" if $op =~ /row IDs/i;
        $label .= " (filesort)" if $op =~ /filesort/i;
    }
    elsif ($op =~ /^Nested loop/i) {
        if ($op =~ /inner/i) { $label = "NESTED LOOP INNER"; }
        elsif ($op =~ /left/i) { $label = "NESTED LOOP LEFT"; }
        elsif ($op =~ /semi/i) { $label = "NESTED LOOP SEMI"; }
        elsif ($op =~ /anti/i) { $label = "NESTED LOOP ANTI"; }
        else { $label = "NESTED LOOP"; }
    }
    elsif ($op =~ /^Aggregate/i) { $label = "AGGREGATE"; }
    elsif ($op =~ /^Group/i) { $label = "GROUP"; }
    elsif ($op =~ /^Materialize/i) { $label = "MATERIALIZE"; }
    elsif ($op =~ /^Stream results/i) { $label = "STREAM"; }
    elsif ($op =~ /^Limit/i) { $label = "LIMIT"; }
    elsif ($op =~ /^Intersect/i) { $label = "Intersect rows sorted by row ID"; }
    elsif ($op =~ /^Union/i) { $label = "UNION"; }
    else {
        $label = $op;
        $label = substr($label, 0, 60) . ".." if length($label) > 62;
    }
    my @metrics;
    push @metrics, "starts=$loops" if defined $loops;
    push @metrics, sprintf("rows=%.0f", $rows) if defined $rows;
    $label .= " " . join(" ", @metrics) if @metrics;
    $label =~ s/;/_/g;
    return $label;
}

sub parse_node {
    my ($node) = @_;
    return undef unless ref $node eq 'HASH';

    my $op = $node->{operation} // 'unknown';
    $op =~ s/`//g;
    $op =~ s/DATE'(\d{4}-\d{2}-\d{2})'/$1/g;

    my $loops = $node->{actual_loops} // 1;
    my $total_time = ($node->{actual_last_row_ms} // 0) * $loops;
    my @children_refs = exists $node->{inputs} && ref $node->{inputs} eq 'ARRAY' ? @{$node->{inputs}} : ();

    my @children = ();
    my $children_time = 0;
    foreach my $c (sort {
        (($b->{actual_last_row_ms} // 0) * ($b->{actual_loops} // 1)) <=>
        (($a->{actual_last_row_ms} // 0) * ($a->{actual_loops} // 1))
    } @children_refs) {
        my $child = parse_node($c);
        next unless $child;
        push @children, $child;
        $children_time += $child->{total_time};
    }

    my $self_time = $total_time - $children_time;
    $self_time = 0 if $self_time < 0;

    my $short = build_short_label($op, $node->{table_name}, $node->{index_name}, $node->{condition});
    my $folded = build_folded_label($node);

    my $details = {
        operation      => $node->{operation} // '',
        table_name     => $node->{table_name} // '',
        index_name     => $node->{index_name} // '',
        access_type    => $node->{access_type} // '',
        actual_rows    => $node->{actual_rows},
        actual_loops   => $node->{actual_loops} // 1,
        estimated_rows => $node->{estimated_rows},
        actual_last_row_ms   => $node->{actual_last_row_ms},
        actual_first_row_ms  => $node->{actual_first_row_ms},
        estimated_total_cost => $node->{estimated_total_cost},
        condition      => $node->{condition} // '',
        ranges         => $node->{ranges} // [],
        covering       => $node->{covering},
        schema_name    => $node->{schema_name} // '',
    };

    return {
        short_label  => $short,
        folded_label => $folded,
        full_label   => $op,
        details      => $details,
        self_time    => $self_time,
        total_time   => $total_time,
        rows         => $node->{actual_rows} // 0,
        loops        => $loops,
        children     => \@children,
    };
}

# Parse once for all types
my $root = parse_node($data);
die "Failed to parse EXPLAIN JSON\n" unless $root;

#-------------------------------------------------------------------------------
# Derived data from unified tree
#-------------------------------------------------------------------------------

# Flamegraph: list of { path => [folded_labels], time => self_time }
sub build_flame_entries {
    my ($node, $path_ref) = @_;
    my @path = @$path_ref;
    push @path, $node->{folded_label};
    my @out = ();
    push @out, { path => \@path, time => $node->{self_time} };
    foreach my $child (@{$node->{children}}) {
        push @out, build_flame_entries($child, \@path);
    }
    return @out;
}

# Bargraph: flat list of nodes (for bar chart)
sub flatten_nodes {
    my ($node) = @_;
    my @out = ($node);
    foreach my $child (@{$node->{children}}) {
        push @out, flatten_nodes($child);
    }
    return @out;
}

# Treemap: list of nodes in tree order (we layout by total_time)
# Tree is already parsed; we just need layout.

#-------------------------------------------------------------------------------
# Unit and multiplier (shared)
#-------------------------------------------------------------------------------
my $max_time = $root->{total_time};
my $use_microseconds = ($max_time > 0 && $max_time < 1);
my $unit = $use_microseconds ? "us" : "ms";
my $unit_display = $use_microseconds ? "µs" : "ms";
my $multiplier = $use_microseconds ? 1000 : 1;

#-------------------------------------------------------------------------------
# Output: Flamegraph
#-------------------------------------------------------------------------------
if ($type eq 'flamegraph') {
    my @entries = build_flame_entries($root, []);
    my $folded_output = "";
    foreach my $entry (@entries) {
        my $t = $entry->{time} * $multiplier;
        $t = int($t + 0.5);
        $t = 1 if $t == 0 && @{$entry->{path}} == 1;
        next if $t <= 0;
        $folded_output .= join(";", @{$entry->{path}}) . " $t\n";
    }

    my $script_dir = dirname(File::Spec->rel2abs($0));
    my $flamegraph_pl = File::Spec->catfile($script_dir, "flamegraph.pl");
    die "Cannot find flamegraph.pl in $script_dir\n" unless -x $flamegraph_pl;

    my @fg_cmd = ($flamegraph_pl, "--width", $width, "--height", $height, "--colors", $colors, "--title", $title, "--countname", $unit);
    push @fg_cmd, "--inverted" if $inverted;

    my $pid = open2(my $fg_out, my $fg_in, @fg_cmd) or die "Cannot run flamegraph.pl: $!\n";
    print $fg_in $folded_output;
    close $fg_in;
    my $svg = do { local $/; <$fg_out> };
    close $fg_out;
    waitpid($pid, 0);

    if ($enhance_tooltips) {
        my %op_details = map { $_->{folded_label} => $_->{details} } flatten_nodes($root);
        $svg =~ s{<title>([^<]+)</title>}{
            "<title>" . enhance_tooltip_flame($1, \%op_details) . "</title>"
        }ge;
    }
    print $svg;
    exit 0;
}

#-------------------------------------------------------------------------------
# Output: Bargraph
#-------------------------------------------------------------------------------
if ($type eq 'bargraph') {
    my @all = flatten_nodes($root);
    @all = sort { $b->{self_time} <=> $a->{self_time} } @all;
    @all = grep { $_->{self_time} >= 0.001 } @all;

    my $total_time = 0;
    $total_time += $_->{self_time} for @all;
    $total_time = 0.001 if $total_time == 0;

    if ($use_microseconds) {
        $_->{self_time} *= $multiplier for @all;
        $total_time *= $multiplier;
    }

    my $bar_height = 28;
    my $bar_gap = 6;
    my $left_margin = 10;
    my $right_margin = 10;
    my $top_margin = 80;
    my $bottom_margin = 40;
    my $label_width = 320;
    my $loops_width = 80;
    my $time_width = 120;
    my $bar_area_width = $width - $left_margin - $right_margin - $label_width - $loops_width - $time_width - 20;
    my $num_bars = scalar @all;
    my $chart_height = $top_margin + ($num_bars * ($bar_height + $bar_gap)) + $bottom_margin;

    my @colors = ('rgb(255,90,90)', 'rgb(255,130,70)', 'rgb(255,165,50)', 'rgb(255,200,50)', 'rgb(255,220,80)', 'rgb(200,200,100)', 'rgb(150,200,150)', 'rgb(100,180,180)');
    my $col_label_x = $left_margin;
    my $col_loops_x = $left_margin + $label_width;
    my $col_bar_x = $col_loops_x + $loops_width;
    my $col_time_x = $col_bar_x + $bar_area_width + 10;

    sub format_number {
        my ($n) = @_;
        $n = int($n);
        my $s = "$n";
        $s =~ s/(\d)(?=(\d{3})+$)/$1,/g;
        return $s;
    }

    print qq{<?xml version="1.0" standalone="no"?>
<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">
<svg version="1.1" width="$width" height="$chart_height" xmlns="http://www.w3.org/2000/svg">
<style>
  text { font-family: Arial, sans-serif; font-size: 12px; }
  .title { font-size: 18px; font-weight: bold; }
  .subtitle { font-size: 11px; fill: #666; }
  .col-header { font-size: 10px; fill: #999; font-weight: bold; }
  .label { font-size: 11px; }
  .loops { font-size: 10px; fill: #666; }
  .value { font-size: 11px; font-weight: bold; }
  .bar:hover { opacity: 0.8; cursor: pointer; }
</style>
<rect width="100%" height="100%" fill="#fafafa"/>
<text x="${\($width/2)}" y="28" text-anchor="middle" class="title">$title</text>
<text x="${\($width/2)}" y="46" text-anchor="middle" class="subtitle">Self-time per operation | Total: ${\($total_time >= 1 ? sprintf("%.0f", $total_time) : sprintf("%.3f", $total_time))} $unit_display</text>
<text x="${\($col_label_x + $label_width - 10)}" y="68" text-anchor="end" class="col-header">OPERATION</text>
<text x="${\($col_loops_x + $loops_width/2)}" y="68" text-anchor="middle" class="col-header">LOOPS</text>
<text x="${\($col_bar_x + $bar_area_width/2)}" y="68" text-anchor="middle" class="col-header">SELF-TIME</text>
<line x1="$left_margin" y1="72" x2="${\($width - $right_margin)}" y2="72" stroke="#ddd" stroke-width="1"/>
};

    my $y = $top_margin;
    my $i = 0;
    foreach my $op (@all) {
        my $pct = ($op->{self_time} / $total_time) * 100;
        my $bar_width = ($op->{self_time} / $total_time) * $bar_area_width;
        $bar_width = 3 if $bar_width < 3 && $op->{self_time} > 0;
        my $color = $colors[$i % @colors];
        my $label = $op->{short_label};
        $label = substr($label, 0, 45) . "..." if length($label) > 48;
        my $text_y = $y + ($bar_height / 2) + 4;
        my $label_escaped = xml_escape($label);
        my $full_escaped = xml_escape($op->{full_label} // $op->{short_label});
        my $st = $op->{self_time} >= 1 ? sprintf("%.0f", $op->{self_time}) : sprintf("%.3f", $op->{self_time});
        my $loops_t = format_number($op->{loops});
        print qq{<text x="${\($col_label_x + $label_width - 10)}" y="$text_y" text-anchor="end" class="label">$label_escaped</text>\n};
        print qq{<text x="${\($col_loops_x + $loops_width/2)}" y="$text_y" text-anchor="middle" class="loops">$loops_t</text>\n};
        print qq{<rect class="bar" x="$col_bar_x" y="$y" width="$bar_width" height="$bar_height" fill="$color" rx="3" ry="3"><title>$full_escaped\nSelf-time: $st $unit_display (${\(sprintf "%.1f", $pct)}%)\nRows: ${\(sprintf "%.0f", $op->{rows})}\nLoops: $loops_t</title></rect>\n};
        my $value_text = $op->{self_time} >= 1 ? sprintf("%.0f $unit_display (%.1f%%)", $op->{self_time}, $pct) : sprintf("%.3f $unit_display (%.1f%%)", $op->{self_time}, $pct);
        print qq{<text x="$col_time_x" y="$text_y" class="value">$value_text</text>\n};
        $y += $bar_height + $bar_gap;
        $i++;
    }
    print "</svg>\n";
    exit 0;
}

#-------------------------------------------------------------------------------
# Output: Treemap (hierarchical, area = total_time) - interactive: zoom, search, details
#-------------------------------------------------------------------------------
if ($type eq 'treemap') {
    my $top_margin = 70;
    my $pad = 2;
    my $treemap_width = $width - 2 * $pad;
    my $treemap_height = 600;
    my $chart_height = $top_margin + $treemap_height + 20;

    # Slice layout: divide rect among children by total_time
    sub layout_treemap {
        my ($node, $x, $y, $w, $h, $depth, $results_ref) = @_;
        return if $w < 4 || $h < 4;
        push @$results_ref, { x => $x, y => $y, w => $w, h => $h, node => $node, depth => $depth };

        my @children = @{$node->{children}};
        return unless @children;

        my $total = 0;
        $total += $_->{total_time} for @children;
        return if $total <= 0;

        my $ax = $x;
        my $ay = $y;
        my $aw = $w;
        my $ah = $h;
        if ($w >= $h) {
            for my $i (0 .. $#children) {
                my $frac = $children[$i]{total_time} / $total;
                my $cw = ($i == $#children) ? ($w - ($ax - $x)) : int($w * $frac + 0.5);
                layout_treemap($children[$i], $ax, $ay, $cw, $ah, $depth + 1, $results_ref);
                $ax += $cw;
            }
        } else {
            for my $i (0 .. $#children) {
                my $frac = $children[$i]{total_time} / $total;
                my $ch = ($i == $#children) ? ($h - ($ay - $y)) : int($h * $frac + 0.5);
                layout_treemap($children[$i], $ax, $ay, $aw, $ch, $depth + 1, $results_ref);
                $ay += $ch;
            }
        }
    }

    my @rects = ();
    layout_treemap($root, $pad, $top_margin, $treemap_width, $treemap_height, 0, \@rects);

    my @tm_colors = (
        'rgb(255,99,71)', 'rgb(255,160,122)', 'rgb(255,218,185)',
        'rgb(176,224,230)', 'rgb(135,206,250)', 'rgb(173,216,230)',
        'rgb(144,238,144)', 'rgb(152,251,152)', 'rgb(255,250,205)',
    );

    # Escape for use in HTML attribute (so JS can read)
    sub attr_escape {
        my ($s) = @_;
        return '' unless defined $s;
        $s =~ s/&/&amp;/g;
        $s =~ s/</&lt;/g;
        $s =~ s/>/&gt;/g;
        $s =~ s/"/&quot;/g;
        $s =~ s/\r?\n/&#10;/g;
        return $s;
    }

    print qq{<?xml version="1.0" standalone="no"?>
<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">
<svg version="1.1" width="$width" height="$chart_height" onload="init(evt)" xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">
<style>
  text { font-family: Arial, sans-serif; font-size: 11px; }
  .title { font-size: 18px; font-weight: bold; }
  .subtitle { font-size: 11px; fill: #666; }
  .treemap-cell { stroke: #fff; stroke-width: 1; cursor: pointer; }
  .treemap-cell:hover { opacity: 0.9; stroke: #333; stroke-width: 1.5; }
  .treemap-cell.highlight { stroke: rgb(230,0,230); stroke-width: 2; }
  .treemap-cell.zoomed { stroke: #111; stroke-width: 2; }
  #unzoom, #search { font-size: 12px; fill: #666; cursor: pointer; }
  #unzoom:hover, #search:hover { fill: #000; }
  #unzoom.hide { display: none; }
  #details { font-size: 11px; fill: #333; }
</style>
<rect width="100%" height="100%" fill="#fafafa"/>
<text x="${\($width/2)}" y="26" text-anchor="middle" class="title">$title</text>
<text x="${\($width/2)}" y="48" text-anchor="middle" class="subtitle">Treemap by total time (hierarchy) | Total: ${\($root->{total_time} >= 1 ? sprintf("%.0f", $root->{total_time}) : sprintf("%.3f", $root->{total_time}))} $unit_display</text>
<text id="unzoom" class="hide" x="$pad" y="64" text-anchor="start">Reset Zoom</text>
<text id="search" x="${\($width - $pad - 60)}" y="64" text-anchor="end">Search</text>
<text id="details" x="$pad" y="66" text-anchor="start">Click a cell to zoom; Ctrl+F to search</text>
<g id="zoomable">
};

    my $cell_id = 0;
    foreach my $r (@rects) {
        my $n = $r->{node};
        my $x = $r->{x};
        my $y = $r->{y};
        my $w = $r->{w};
        my $h = $r->{h};
        my $d = $r->{depth};
        next if $w < 8 || $h < 8;
        my $color = $tm_colors[$d % @tm_colors];
        my $short_label = $n->{short_label};
        my $label = $short_label;
        $label = substr($label, 0, 25) . "..." if length($label) > 28;
        my $st = $n->{self_time} >= 1 ? sprintf("%.0f", $n->{self_time}) : sprintf("%.3f", $n->{self_time});
        my $tt = $n->{total_time} >= 1 ? sprintf("%.0f", $n->{total_time}) : sprintf("%.3f", $n->{total_time});
        my $title_text = "$n->{short_label}\nSelf: $st $unit_display | Total: $tt $unit_display\nRows: $n->{rows} | Loops: $n->{loops}";
        my $info_attr = attr_escape($title_text);
        my $label_attr = attr_escape($short_label);
        print qq{<rect id="cell-$cell_id" class="treemap-cell" x="$x" y="$y" width="$w" height="$h" fill="$color" data-x="$x" data-y="$y" data-w="$w" data-h="$h" data-label="$label_attr" data-info="$info_attr"><title>} . xml_escape($title_text) . qq{</title></rect>\n};
        if ($w > 40 && $h > 14) {
            my $tx = $x + 4;
            my $ty = $y + $h/2 + 3;
            print qq{<text x="$tx" y="$ty" fill="#333" font-size="10" pointer-events="none">} . xml_escape($label) . qq{</text>\n};
        }
        $cell_id++;
    }

    print qq{</g>
<script type="text/ecmascript"><![CDATA[
(function() {
  var pad = $pad, topMargin = $top_margin, tmWidth = $treemap_width, tmHeight = $treemap_height, svgWidth = $width, svgHeight = $chart_height;
  var zoomable, unzoomBtn, searchBtn, detailsEl;
  var zoomState = { scale: 1, tx: pad, ty: topMargin };

  function init(evt) {
    zoomable = document.getElementById("zoomable");
    unzoomBtn = document.getElementById("unzoom");
    searchBtn = document.getElementById("search");
    detailsEl = document.getElementById("details");
    if (!zoomable) return;
    zoomState = { scale: 1, tx: 0, ty: 0 };
    document.addEventListener("click", function(e) {
      var t = e.target;
      if (t.id === "unzoom") { resetZoom(); return; }
      if (t.id === "search") { searchPrompt(); return; }
      if (t.classList && t.classList.contains("treemap-cell")) {
        var x = parseFloat(t.getAttribute("data-x")), y = parseFloat(t.getAttribute("data-y")), w = parseFloat(t.getAttribute("data-w")), h = parseFloat(t.getAttribute("data-h"));
        if (zoomState.scale !== 1 && t.classList.contains("zoomed")) { resetZoom(); return; }
        zoomTo(x, y, w, h);
        t.classList.add("zoomed");
        detailsEl.textContent = t.getAttribute("data-info").replace(/&#10;/g, " | ");
      }
    });
    document.addEventListener("mouseover", function(e) {
      if (e.target.classList && e.target.classList.contains("treemap-cell"))
        detailsEl.textContent = e.target.getAttribute("data-info").replace(/&#10;/g, " | ");
    });
    document.addEventListener("keydown", function(e) {
      if (e.ctrlKey && e.key === "f") { e.preventDefault(); searchPrompt(); }
    });
  }
  function zoomTo(x, y, w, h) {
    var scale = Math.min(tmWidth / w, tmHeight / h);
    var tx = pad - x * scale;
    var ty = topMargin - y * scale;
    zoomable.setAttribute("transform", "translate(" + tx + "," + ty + ") scale(" + scale + ")");
    zoomState = { scale: scale, tx: tx, ty: ty };
    unzoomBtn.classList.remove("hide");
  }
  function resetZoom() {
    zoomable.setAttribute("transform", "");
    zoomState = { scale: 1, tx: 0, ty: 0 };
    unzoomBtn.classList.add("hide");
    var cells = document.querySelectorAll(".treemap-cell.zoomed");
    for (var i = 0; i < cells.length; i++) cells[i].classList.remove("zoomed");
    detailsEl.textContent = "Click a cell to zoom; Ctrl+F to search";
  }
  function searchPrompt() {
    if (searchBtn.textContent === "Reset Search") {
      var cells = document.querySelectorAll(".treemap-cell");
      for (var i = 0; i < cells.length; i++) cells[i].classList.remove("highlight");
      searchBtn.textContent = "Search";
      return;
    }
    var term = prompt("Search (regex):");
    if (term == null) return;
    var re;
    try { re = new RegExp(term, "i"); } catch (err) { alert("Invalid regex"); return; }
    var cells = document.querySelectorAll(".treemap-cell");
    for (var i = 0; i < cells.length; i++) {
      var label = cells[i].getAttribute("data-label") || "";
      cells[i].classList.toggle("highlight", re.test(label));
    }
    searchBtn.textContent = "Reset Search";
  }
  window.init = init;
})();
]]></script>
</svg>
};
    exit 0;
}
